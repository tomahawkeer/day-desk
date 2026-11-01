"""Decision score: did Claude's buy/pass FILTER add anything? Scored in hindsight on every entry judgment.

Run DECISION_SCORE.bat (or: python decision_score.py). It reads data/decisions.jsonl and data/bars/*.json,
prints a plain-English report and saves data/decision_score.txt and data/decision_score.json.

Idea (from QuantConnect Lean's Insight scoring): Claude is woken ~13 times a day and mostly says PASS; only ~3 buys
a day become trades. Waiting for 60 closed trades to learn anything takes weeks. Instead, every time Claude was asked
"buy or pass?" about a stock it did not hold, this file asks: what would the SAME neutral trade have done from that
moment? If buys beat passes on the same yardstick, the filter is adding something; if not, it is not (yet).

Neutral bracket (the same for buys and passes, so Claude's own stop/target never tilts the comparison):
  entry  = the OPEN of the first 1-minute bar that opened after the decision
  typical= mean (high - low) / open of the stock's 1-minute bars from 9:30 up to the decision
  stop   = entry x (1 - 2 x typical), never further than config max_stop_pct
  target = entry + 1.5 x (entry - stop)
  walked with lab.walk_exits (engine fill rules: gaps fill at the open, a bar touching both = stop), anything still
  open sold at the close of the last bar before config flatten_minute (3:45), engine.COST_PER_SIDE paid each side.
  R = net profit per share / (entry - stop).
  These yardstick constants resemble the desk's median stop (~1.8 typical ranges, trade lab) and the prompt's 1.5x
  reward rule; they were NOT tuned on these results and must not be.

DIAGNOSTIC ONLY. This is never part of the pre-registered grade (grade_read.py). It is hindsight; judgments in the
same stock and hour are correlated, so the effective sample is smaller than n. Zero Claude calls, no network.

Rulings (10-31 build):
  - An entry judgment = a row whose trigger is a scanner kind (anything except the engine's NOT_CLAUDE_CALLS rows,
    "review", "self-test", "morning research", "end-of-day review"), action "buy" or "pass", with a symbol.
    hold/sell/adjust under a scanner trigger are position management on a stock already held: not entry judgments.
    A pass whose result is "holding" (stock already held) is skipped for the same reason.
  - A buy counts only if its result starts with "bought"; a refused buy is skipped (the engine, not Claude, decided).
  - No bar after the decision before 3:45, or no measurable range (typical 0) -> skipped, counted in "skipped".
  - fwd30/fwd60 = stock % from the entry open to the close of the bar N minutes later (the last bar opening before
    entry + N min, which must be at least N-1 minutes in) minus the same for QQQ; None when the bars do not reach.
  - Bootstrap: 2,000 resamples of whole trading days with replacement (seed 20261031); a resample with no buys or no
    passes is skipped (it simply does not count). 90% CI = 5th/95th percentile (nearest rank).
  - With fewer than MIN_BOOT_DAYS (5) days, a day-block CI that excludes 0 is NOT called for either side: 3 days give
    only 10 distinct resamples, so the interval is far too coarse (10-31 real data: [-0.90, -0.45] around -0.47).
  - The yardstick constants live here, not in config.json (like lab.py's MULTS): they are a fixed measuring stick,
    not a trading setting tomahawkeer tunes.
  - CONTROL ARM (pre-reg Addendum A4, 10-31 evening build): see the "Addendum A4" block below. Same candidates
    (not-held scanner decisions, buy or pass, from 11-02), same neutral bracket, three ways of choosing: ALL / GATED
    (close > session VWAP AND stronger than QQQ since the open) / CLAUDE (buys). Reported only; grade_read.py adds the
    pre-written reading at Read 1/Read 2.
  - p_win calibration: outcome 1 = neutral bracket hit the target first, 0 = stop or flatten. Bins 0-40, 40-60,
    60-100 (the top bin includes 100). Shown once >= 10 scored rows carry p_win.
"""
from __future__ import annotations

import json
import os
import random
import statistics
from datetime import datetime

import lab
from engine import COST_PER_SIDE, ET, hm, minutes_et
from health import write_json

HERE = os.path.dirname(os.path.abspath(__file__))

# Yardstick: resembles the desk's median stop ~1.8 typical ranges and the prompt's 1.5x rule; NOT tuned.
STOP_TYPICAL_RANGES = 2.0
TARGET_R = 1.5
FWD_MINUTES = (30, 60)
BOOT_N = 2000
BOOT_SEED = 20261031
CI_LO, CI_HI = 0.05, 0.95                # 90% interval
MIN_BOOT_DAYS = 5                        # below this many days a day-block interval is too coarse to call a side
MIN_CALIBRATION_ROWS = 10
P_BINS = [(0, 40), (40, 60), (60, 100)]  # last bin includes 100
REVIEW_MAX_LINES = 5
REVIEW_MAX_CHARS = 700
HINDSIGHT_TAIL = "Hindsight — a clue, not proof."

# Rows that are not entry judgments. NOT_CLAUDE_CALLS is grade_read's list (pinned identical to day_desk's by a test).
from grade_read import NOT_CLAUDE_CALLS   # noqa: E402
NON_ENTRY_TRIGGERS = set(NOT_CLAUDE_CALLS) | {"review", "self-test", "morning research", "end-of-day review"}


# ---------------------------------------------------------------- selection
def load_rows(folder: str) -> list[dict]:
    p = os.path.join(folder, "data", "decisions.jsonl")
    out = []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def is_entry_judgment(r: dict) -> bool:
    trig, act = r.get("trigger"), r.get("action")
    if not trig or trig in NON_ENTRY_TRIGGERS or not r.get("sym") or r.get("sym") == "-" or "t" not in r:
        return False
    res = str(r.get("result") or "")
    if act == "buy":
        return res.startswith("bought")
    if act == "pass":
        return res != "holding"
    return False                          # "-", error, hold, sell, adjust, anything else


def judgments(rows: list[dict]) -> list[dict]:
    return [r for r in rows if is_entry_judgment(r)]


# ---------------------------------------------------------------- the neutral bracket
def typical_range(bars: list[dict], t: float) -> float | None:
    """Mean (h-l)/o (a fraction) of the bars from 9:30 that had closed by t; else the bar containing t."""
    morning = [b for b in bars if b["t"] + 60 <= t and minutes_et(b["t"]) >= lab.MARKET_OPEN_MINUTE and b["o"]]
    if not morning:
        morning = [b for b in bars if b["t"] <= t and b["o"]][-1:]
    if not morning:
        return None
    return statistics.fmean([(b["h"] - b["l"]) / b["o"] for b in morning])


def _fwd(bars: list[dict], entry_bar: dict, minutes: int) -> float | None:
    end_t = entry_bar["t"] + minutes * 60
    within = [b for b in bars if entry_bar["t"] <= b["t"] < end_t]
    if not within or within[-1]["t"] < entry_bar["t"] + (minutes - 1) * 60:
        return None
    return (within[-1]["c"] / entry_bar["o"] - 1) * 100


def fwd_vs_qqq(bars: list[dict], qqq: list[dict], t: float, minutes: int) -> float | None:
    e = next((b for b in bars if b["t"] > t), None)
    q = next((b for b in qqq if b["t"] > t), None)
    if e is None or q is None:
        return None
    a, b = _fwd(bars, e, minutes), _fwd(qqq, q, minutes)
    return None if a is None or b is None else round(a - b, 4)


def neutral_bracket(bars: list[dict], t: float, cfg: dict) -> dict | None:
    """Score one judgment made at time t on its stock's 1-minute bars. None if it cannot be scored."""
    bars = sorted(bars or [], key=lambda b: b["t"])
    flat = lab._flat_bars(bars, cfg["flatten_minute"])
    later = [b for b in flat if b["t"] > t]
    if not later:
        return None
    entry_bar = later[0]
    entry = entry_bar["o"]
    typ = typical_range(bars, t)
    if not typ or typ <= 0 or not entry:
        return None
    cap = float(cfg.get("max_stop_pct", lab.DEFAULT_MAX_STOP_PCT)) / 100
    dist_frac = min(STOP_TYPICAL_RANGES * typ, cap)
    stop = entry * (1 - dist_frac)
    risk = entry - stop
    target = entry + TARGET_R * risk
    hit = lab.walk_exits(later, stop, target, t)            # entry bar included: it opened after t
    if hit:
        px, reason, xt = hit["price"], hit["reason"], hit["t"]
    else:
        px, reason, xt = later[-1]["c"], "flatten", later[-1]["t"] + 60
    net = px * (1 - COST_PER_SIDE) - entry * (1 + COST_PER_SIDE)
    return {"entry": round(entry, 4), "entry_t": entry_bar["t"], "typical_pct": round(typ * 100, 4),
            "stop": round(stop, 4), "target": round(target, 4), "clamped": STOP_TYPICAL_RANGES * typ > cap,
            "exit": round(px, 4), "exit_t": xt, "outcome": reason, "r": round(net / risk, 4)}


def score_all(folder: str) -> tuple[list[dict], list[dict]]:
    """(scored rows, skipped rows) for every entry judgment in the log."""
    cfg = lab.load_config(folder)
    cache: dict[str, dict] = {}
    scored, skipped = [], []
    for r in judgments(load_rows(folder)):
        t = float(r["t"])
        day = lab.day_of(t)
        if day not in cache:
            cache[day] = lab.load_bars(folder, day)
        bars = cache[day].get(r["sym"]) or []
        base = {"day": day, "time": hm(t), "t": t, "sym": r["sym"], "trigger": r["trigger"], "action": r["action"]}
        nb = neutral_bracket(bars, t, cfg) if bars else None
        if nb is None:
            skipped.append({**base, "why": "no saved bars" if not bars else "no bar after it before 3:45 / no range"})
            continue
        qqq = cache[day].get("QQQ") or []
        row = {**base, **nb}
        for m in FWD_MINUTES:
            row[f"fwd{m}_vs_qqq"] = fwd_vs_qqq(sorted(bars, key=lambda b: b["t"]), sorted(qqq, key=lambda b: b["t"]), t, m)
        pw = r.get("p_win")
        row["p_win"] = pw if isinstance(pw, (int, float)) and not isinstance(pw, bool) and 0 <= pw <= 100 else None
        scored.append(row)
    return scored, skipped


# ---------------------------------------------------------------- statistics
def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 4) if xs else None


def group_stats(rows: list[dict]) -> dict:
    f30 = [r["fwd30_vs_qqq"] for r in rows if r.get("fwd30_vs_qqq") is not None]
    return {"n": len(rows), "mean_r": _mean([r["r"] for r in rows]),
            "target_first_pct": round(100 * sum(r["outcome"] == "target" for r in rows) / len(rows), 1) if rows else None,
            "stop_first": sum(r["outcome"] == "stop" for r in rows),
            "mean_fwd30_vs_qqq": _mean(f30), "n_fwd30": len(f30)}


def _diff(rows: list[dict]) -> float | None:
    b = [r["r"] for r in rows if r["action"] == "buy"]
    p = [r["r"] for r in rows if r["action"] == "pass"]
    if not b or not p:
        return None
    return statistics.fmean(b) - statistics.fmean(p)


def _nearest_rank(sorted_xs: list[float], q: float) -> float:
    k = max(1, int(-(-q * len(sorted_xs) // 1)))          # ceil(q*n), at least 1
    return sorted_xs[min(k, len(sorted_xs)) - 1]


def day_bootstrap(rows: list[dict], n: int = BOOT_N, seed: int = BOOT_SEED) -> dict:
    """BUY minus PASS mean R, with a 90% CI from resampling WHOLE DAYS (rows in a day stay together)."""
    point = _diff(rows)
    days: dict[str, list[dict]] = {}
    for r in rows:
        days.setdefault(r["day"], []).append(r)
    keys = sorted(days)
    out = {"diff": round(point, 4) if point is not None else None, "n_days": len(keys), "ci90": None,
           "resamples_used": 0, "verdict": "not enough (need buys and passes)"}
    if point is None:
        return out
    if len(keys) < 2:
        out["verdict"] = "one day only: no interval"
        return out
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        sample = [r for _k in range(len(keys)) for r in days[keys[rng.randrange(len(keys))]]]
        d = _diff(sample)
        if d is not None:
            diffs.append(d)
    if not diffs:
        return out
    diffs.sort()
    lo, hi = _nearest_rank(diffs, CI_LO), _nearest_rank(diffs, CI_HI)
    if lo <= 0 <= hi:
        verdict = "within noise"
    elif len(keys) < MIN_BOOT_DAYS:
        verdict = f"too few days to call ({len(keys)} < {MIN_BOOT_DAYS}): treat as within noise"
    else:
        verdict = "buys ahead (still hindsight)" if lo > 0 else "passes ahead (still hindsight)"
    out.update(ci90=[round(lo, 4), round(hi, 4)], resamples_used=len(diffs), verdict=verdict)
    return out


def calibration(rows: list[dict]) -> dict:
    pr = [r for r in rows if r.get("p_win") is not None]
    if len(pr) < MIN_CALIBRATION_ROWS:
        return {"n": len(pr), "ready": False, "note": f"not enough yet ({len(pr)} of {MIN_CALIBRATION_ROWS} rows carry p_win)"}
    bins = []
    for lo, hi in P_BINS:
        sel = [r for r in pr if lo <= r["p_win"] < hi or (hi == 100 and r["p_win"] == 100)]
        ys = [1.0 if r["outcome"] == "target" else 0.0 for r in sel]
        bins.append({"bin": f"{lo}-{hi}", "n": len(sel),
                     "mean_p_win": _mean([r["p_win"] for r in sel]),
                     "target_first_pct": round(100 * statistics.fmean(ys), 1) if sel else None,
                     "brier": _mean([(r["p_win"] / 100 - y) ** 2 for r, y in zip(sel, ys)])})
    allb = _mean([(r["p_win"] / 100 - (1.0 if r["outcome"] == "target" else 0.0)) ** 2 for r in pr])
    return {"n": len(pr), "ready": True, "bins": bins, "brier_all": allb}


def summarize(rows: list[dict]) -> dict:
    by_action = {a: group_stats([r for r in rows if r["action"] == a]) for a in ("buy", "pass")}
    by_trigger = {}
    for trig in sorted({r["trigger"] for r in rows}):
        sel = [r for r in rows if r["trigger"] == trig]
        by_trigger[trig] = {"buy": group_stats([r for r in sel if r["action"] == "buy"]),
                            "pass": group_stats([r for r in sel if r["action"] == "pass"]),
                            "buy_minus_pass": day_bootstrap(sel)}
    passes = [r for r in rows if r["action"] == "pass"]
    return {"by_action": by_action, "buy_minus_pass": day_bootstrap(rows), "by_trigger": by_trigger,
            "dodged_losers": sum(r["outcome"] == "stop" for r in passes),
            "missed_winners": sum(r["outcome"] == "target" for r in passes),
            "calibration": calibration(rows)}


# ---------------------------------------------------------------- Addendum A4: the control arm (reported only)
# Pre-registration Addendum A4 (written Sat 10-31 ~5:45 PM, before any data it is scored on). Three arms take from the
# SAME candidates and are scored with the SAME neutral bracket above, so they differ only in WHICH candidates they take:
#   ALL    = every candidate;
#   GATED  = the fixed two-line rule: on the last FINISHED 1-minute bar at the decision time, close > session VWAP
#            AND the stock's % since the session open > QQQ's % since its open over the same bars;
#   CLAUDE = the candidates Claude answered "buy".
# Reported, never graded. Between reads: running numbers only, no reading. At Read 1 / Read 2 grade_read.py adds the
# pre-written reading (a4_reading). The rule is never changed after seeing results; a new rule = a new addendum.
#
# Rulings made in code before any candidate existed (10-31 evening build):
#  - Candidate = trigger in A4_TRIGGERS, a real symbol, NOT held, answered buy or pass:
#      pass -> result "no trade" ("holding" means the stock was held);
#      buy  -> result "bought ..." OR "refused: ..." (Claude answered buy; the engine refusing it does not change
#              what Claude chose, and dropping refusals would let the engine's limits edit the CLAUDE arm);
#      anything else (hold/sell/adjust/error/skipped/"ignored: ... (held)") is not a candidate.
#    Rows logged after this build carry held=true/false; held=true is never a candidate.
#  - A candidate the neutral bracket cannot score (no saved bars, no bar after it before 3:45) is dropped from ALL
#    arms (count reported), so the arms always share one candidate list.
#  - Session = the stock's bars from 9:30 that had finished by the decision time (t + 60 <= decision t). VWAP is
#    scanner.vwap (typical price x volume) over that session, the same formula the scanner and prompt use.
#    QQQ uses its session bars that opened at or before the stock's last finished bar ("over the same bars").
#  - If the gate cannot be computed (no finished stock bar, zero volume so no VWAP, no QQQ bar, a zero open) the
#    GATED arm does NOT take that candidate; the count is reported as gate_unknown. A missing number never says yes.
#  - Intervals: the same whole-day bootstrap as above (BOOT_N, BOOT_SEED, nearest-rank 90%). Below MIN_BOOT_DAYS the
#    interval is shown with a "too few days" note.
A4_START_DAY = "2026-11-02"               # Addendum A4: candidates from Mon 11-02 onward
A4_TRIGGERS = ("momentum", "flush_bounce", "vwap_reclaim", "breakout")   # the scanner kinds named in A4
A4_ARMS = ("ALL", "GATED", "CLAUDE")
A4_NOTE = ("Pre-reg Addendum A4, reported only: never changes the grade. Running numbers between reads; the "
           "pre-written reading is applied only at Read 1 and Read 2.")


def is_a4_candidate(r: dict) -> bool:
    if r.get("trigger") not in A4_TRIGGERS or not r.get("sym") or r.get("sym") == "-" or "t" not in r:
        return False
    if r.get("held") is True:
        return False
    res = str(r.get("result") or "")
    if r.get("action") == "buy":
        return res.startswith("bought") or res.startswith("refused")
    if r.get("action") == "pass":
        return res == "no trade"
    return False


def _session_finished(bars: list[dict], t: float) -> list[dict]:
    return [b for b in sorted(bars or [], key=lambda b: b["t"])
            if b["t"] + 60 <= t and minutes_et(b["t"]) >= lab.MARKET_OPEN_MINUTE]


def a4_gate(bars: list[dict], qqq: list[dict], t: float) -> tuple[bool | None, dict]:
    """The GATED arm's rule, exactly as written in Addendum A4. (True/False, detail) or (None, why) if it can't say."""
    from scanner import vwap
    s = _session_finished(bars, t)
    if not s:
        return None, {"why": "no finished stock bar"}
    last = s[-1]
    q = [b for b in _session_finished(qqq, t) if b["t"] <= last["t"]]
    if not q:
        return None, {"why": "no QQQ bar"}
    vw = vwap(s)
    if vw is None:
        return None, {"why": "no volume, no VWAP"}
    if not s[0]["o"] or not q[0]["o"]:
        return None, {"why": "zero open price"}
    sp = (last["c"] / s[0]["o"] - 1) * 100
    qp = (q[-1]["c"] / q[0]["o"] - 1) * 100
    above = last["c"] > vw
    stronger = sp > qp
    return (above and stronger), {"close": last["c"], "vwap": round(vw, 4), "stock_pct": round(sp, 4),
                                  "qqq_pct": round(qp, 4), "above_vwap": above, "beats_qqq": stronger}


def a4_candidates(folder: str, start_day: str = A4_START_DAY, end_day: str | None = None) -> tuple[list[dict], list[dict]]:
    """(scored candidates, dropped candidates). Each scored row carries the neutral bracket, `claude` and `gated`."""
    cfg = lab.load_config(folder)
    cache: dict[str, dict] = {}
    scored, dropped = [], []
    for r in load_rows(folder):
        if not is_a4_candidate(r):
            continue
        t = float(r["t"])
        day = lab.day_of(t)
        if day < start_day or (end_day and day > end_day):
            continue
        if day not in cache:
            cache[day] = lab.load_bars(folder, day)
        bars = cache[day].get(r["sym"]) or []
        base = {"day": day, "time": hm(t), "t": t, "sym": r["sym"], "trigger": r["trigger"], "action": r["action"],
                "claude": r["action"] == "buy"}
        nb = neutral_bracket(bars, t, cfg) if bars else None
        if nb is None:
            dropped.append({**base, "why": "no saved bars" if not bars else "no bar after it before 3:45 / no range"})
            continue
        g, detail = a4_gate(bars, cache[day].get("QQQ") or [], t)
        scored.append({**base, **nb, "gated": bool(g), "gate_known": g is not None, "gate": detail})
    return scored, dropped


def _arm(rows: list[dict], arm: str) -> list[dict]:
    if arm == "ALL":
        return rows
    key = "gated" if arm == "GATED" else "claude"
    return [r for r in rows if r[key]]


def _mean_r(rows: list[dict]) -> float | None:
    return statistics.fmean([r["r"] for r in rows]) if rows else None


def _c_minus_g(rows: list[dict]) -> float | None:
    c, g = _mean_r(_arm(rows, "CLAUDE")), _mean_r(_arm(rows, "GATED"))
    return None if c is None or g is None else c - g


def day_boot_stat(rows: list[dict], stat, n: int = BOOT_N, seed: int = BOOT_SEED) -> dict:
    """90% interval of stat(rows) from resampling WHOLE days. Resamples where stat is None are skipped (counted)."""
    days: dict[str, list[dict]] = {}
    for r in rows:
        days.setdefault(r["day"], []).append(r)
    keys = sorted(days)
    out = {"n_days": len(keys), "ci90": None, "resamples_used": 0, "note": ""}
    if len(keys) < 2:
        out["note"] = "fewer than 2 days: no interval"
        return out
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        v = stat([r for _k in range(len(keys)) for r in days[keys[rng.randrange(len(keys))]]])
        if v is not None:
            vals.append(v)
    if not vals:
        out["note"] = "no usable resamples"
        return out
    vals.sort()
    out.update(ci90=[round(_nearest_rank(vals, CI_LO), 4), round(_nearest_rank(vals, CI_HI), 4)],
               resamples_used=len(vals),
               note=(f"only {len(keys)} days (< {MIN_BOOT_DAYS}): interval far too coarse to read" if
                     len(keys) < MIN_BOOT_DAYS else ""))
    return out


def control_arms(folder: str, start_day: str = A4_START_DAY, end_day: str | None = None) -> dict:
    rows, dropped = a4_candidates(folder, start_day, end_day)
    out = {"addendum": "A4", "start_day": start_day, "end_day": end_day, "note": A4_NOTE,
           "rule": "GATED takes a candidate only if, on the last finished 1-min bar, close > session VWAP AND the "
                   "stock's % since the open > QQQ's % since the open.",
           "n_candidates": len(rows) + len(dropped), "n_scored": len(rows), "n_dropped": len(dropped),
           "gate_unknown": sum(not r["gate_known"] for r in rows),
           "days": sorted({r["day"] for r in rows}), "arms": {}, "claude_minus_gated": None, "overlap": None}
    for arm in A4_ARMS:
        sel = _arm(rows, arm)
        st = group_stats(sel)
        boot = day_boot_stat(rows, lambda s, a=arm: _mean_r(_arm(s, a)))
        out["arms"][arm] = {"n": st["n"], "mean_r": st["mean_r"], "target_first_pct": st["target_first_pct"],
                            "ci90": boot["ci90"], "n_days": len({r["day"] for r in sel}), "interval_note": boot["note"]}
    d = _c_minus_g(rows)
    b = day_boot_stat(rows, _c_minus_g)
    out["claude_minus_gated"] = {"diff": round(d, 4) if d is not None else None, "ci90": b["ci90"],
                                 "n_days": b["n_days"], "resamples_used": b["resamples_used"],
                                 "interval_note": b["note"] if d is not None else "needs Claude buys AND gated candidates"}
    both = sum(r["claude"] and r["gated"] for r in rows)
    co = sum(r["claude"] and not r["gated"] for r in rows)
    go = sum(r["gated"] and not r["claude"] for r in rows)
    ne = len(rows) - both - co - go
    out["overlap"] = {"both": both, "claude_only": co, "gated_only": go, "neither": ne,
                      "agree_pct": round(100 * (both + ne) / len(rows), 1) if rows else None}
    out["dropped"] = dropped[:20]
    return out


def a4_reading(ca: dict) -> str:
    """The reading pre-written in Addendum A4. ONLY for Read 1 / Read 2 (grade_read.read_section)."""
    cg = (ca or {}).get("claude_minus_gated") or {}
    ci = cg.get("ci90")
    if not ci:
        return "No reading possible: " + (cg.get("interval_note") or "no interval") + "."
    if ci[0] > 0:
        return "Interval above 0: Claude's judgment adds something a two-line rule does not."
    if ci[1] < 0:
        return "Interval below 0: the two-line rule beats Claude."
    return ("Interval straddles 0: no evidence Claude's judgment adds anything over the rule; the plan usage Claude "
            "costs should be questioned.")


def a4_lines(ca: dict | None) -> list[str]:
    """Plain running numbers (no verdict words: grade_read prints these in PROGRESS ONLY too)."""
    if not ca:
        return ["Control arm (Addendum A4): not built yet."]
    L = [f"CONTROL ARM (Addendum A4, from {ca['start_day']}; reported only, never part of the grade)"]
    if not ca["n_scored"]:
        L.append(f"  No candidates yet ({ca['n_candidates']} logged, {ca['n_dropped']} without saved bars). Candidates "
                 "= every Claude decision on a stock not held, from a scanner wake-up, answered buy or no-buy.")
        return L
    L.append(f"  {ca['n_scored']} candidates over {len(ca['days'])} day(s) ({ca['n_dropped']} without saved bars left "
             f"out of every arm; rule could not be checked on {ca['gate_unknown']}, so GATED skipped those).")
    for arm in A4_ARMS:
        a = ca["arms"][arm]
        ci = f"[{_f(a['ci90'][0])}, {_f(a['ci90'][1])}]" if a["ci90"] else "n/a"
        what = {"ALL": "every candidate", "GATED": "the two-line rule", "CLAUDE": "Claude's buys"}[arm]
        L.append(f"  {arm:<6} ({what:<17}) n={a['n']:>3}  mean R {_f(a['mean_r']):>6}  target 1st "
                 f"{_f(a['target_first_pct'], '.0f', unit='%'):>4}  90% range {ci}")
    cg = ca["claude_minus_gated"]
    ci = f"[{_f(cg['ci90'][0])}, {_f(cg['ci90'][1])}]" if cg["ci90"] else "n/a"
    L.append(f"  CLAUDE - GATED mean R {_f(cg['diff'])}, 90% range {ci} over {cg['n_days']} day(s)"
             + (f"  ({cg['interval_note']})" if cg["interval_note"] else ""))
    o = ca["overlap"]
    L.append(f"  Agreement: both took {o['both']}, only Claude {o['claude_only']}, only the rule {o['gated_only']}, "
             f"neither {o['neither']} ({_f(o['agree_pct'], '.0f', unit='%')} the same call).")
    return L


def header(n: int, d: int) -> str:
    return (f"Hindsight on {n} judgments over {d} days; judgments in the same stock/hour are correlated, so effective "
            f"n is smaller. Diagnostic only — not the grade.")


def report(folder: str = HERE) -> dict:
    rows, skipped = score_all(folder)
    days = sorted({r["day"] for r in rows})
    return {"header": header(len(rows), len(days)), "n": len(rows), "days": days,
            "yardstick": {"stop_typical_ranges": STOP_TYPICAL_RANGES, "target_r": TARGET_R,
                          "cost_per_side": COST_PER_SIDE, "note": "resembles the desk's median stop ~1.8 typical "
                          "ranges and the prompt's 1.5x rule; NOT tuned"},
            **summarize(rows), "control_arms": control_arms(folder), "skipped": skipped, "rows": rows}


# ---------------------------------------------------------------- text
def _f(x, fmt="+.2f", none="n/a", unit="") -> str:
    return none if x is None else format(x, fmt) + unit


def _diff_line(b: dict) -> str:
    if b["ci90"] is None:
        return f"BUY-PASS mean R {_f(b['diff'])} ({b['verdict']})"
    return (f"BUY-PASS mean R {_f(b['diff'])}, 90% CI [{_f(b['ci90'][0])}, {_f(b['ci90'][1])}] over {b['n_days']} days "
            f"-> {b['verdict']}")


def report_text(rep: dict) -> str:
    L = ["DAY DESK DECISION SCORE  (no Claude calls; every buy AND pass scored with the same neutral bracket)",
         "!! " + rep["header"], "",
         f"Neutral bracket: entry = next bar's open; stop = {STOP_TYPICAL_RANGES:g} typical 1-min ranges (capped at "
         f"max_stop_pct); target = {TARGET_R:g}R; engine fill rules; sold at 3:45 if neither; cost {COST_PER_SIDE:.2%}/side.",
         "  (resembles the desk's median stop ~1.8 typical ranges and the prompt's 1.5x rule; NOT tuned)", ""]
    L.append(f"  {'':6} {'n':>4} {'mean R':>8} {'target 1st':>11} {'fwd30 vs QQQ':>13}")
    for a in ("buy", "pass"):
        s = rep["by_action"][a]
        L.append(f"  {a.upper():6} {s['n']:>4} {_f(s['mean_r']):>8} {_f(s['target_first_pct'], '.0f', unit='%'):>11} "
                 f"{_f(s['mean_fwd30_vs_qqq'], unit='%'):>13} (n={s['n_fwd30']})")
    L += ["  " + _diff_line(rep["buy_minus_pass"]), "", "BY TRIGGER"]
    for trig, v in rep["by_trigger"].items():
        b, p = v["buy"], v["pass"]
        L.append(f"  {trig:<13} buys n={b['n']} mean R {_f(b['mean_r'])} | passes n={p['n']} mean R {_f(p['mean_r'])} | "
                 + _diff_line(v["buy_minus_pass"]))
    np_ = rep["by_action"]["pass"]["n"]
    L += ["", f"Passes that would have hit the stop first (dodged losers): {rep['dodged_losers']} of n={np_}",
          f"Passes that would have hit the target first (missed winners): {rep['missed_winners']} of n={np_}", ""]
    c = rep["calibration"]
    if not c["ready"]:
        L.append(f"p_win calibration: {c['note']}.")
    else:
        L.append(f"p_win calibration (n={c['n']}; outcome = neutral target first; Brier all {_f(c['brier_all'], '.3f')}):")
        for b in c["bins"]:
            L.append(f"  p_win {b['bin']:>6}: n={b['n']:>3}  mean p_win {_f(b['mean_p_win'], '.1f')}  "
                     f"actual target-first {_f(b['target_first_pct'], '.0f')}%  Brier {_f(b['brier'], '.3f')}")
    L += [""] + a4_lines(rep.get("control_arms"))
    if rep["skipped"]:
        L += ["", f"Skipped {len(rep['skipped'])}: " + "; ".join(f"{s['sym']} {s['day']} {s['time']} ({s['why']})"
                                                               for s in rep["skipped"][:10])]
    L += ["", "PER JUDGMENT"]
    for r in rep["rows"]:
        L.append(f"  {r['day']} {r['time']:>8} {r['sym']:<5} {r['trigger']:<13} {r['action']:<4} -> {r['outcome']:<7} "
                 f"R {r['r']:+.2f}  fwd30 vs QQQ {_f(r['fwd30_vs_qqq'], unit='%')}  fwd60 {_f(r['fwd60_vs_qqq'], unit='%')}"
                 + (f"  p_win {r['p_win']}" if r.get("p_win") is not None else ""))
    L += ["", HINDSIGHT_TAIL]
    return "\n".join(L) + "\n"


def review_lines(folder: str, day: str) -> str:
    """At most 5 lines / 700 chars for the end-of-day review. "" if nothing has been scored yet."""
    rep = report(folder)
    if not rep["n"]:
        return ""
    today = [r for r in rep["rows"] if r["day"] == day]
    tb = [r for r in today if r["action"] == "buy"]
    tp = [r for r in today if r["action"] == "pass"]
    lines = [f"Decision score ({rep['header']}) Same neutral bracket for every judgment:"]
    if today:
        lines.append(f"Today: buys n={len(tb)} mean R {_f(_mean([r['r'] for r in tb]))}; passes n={len(tp)} mean R "
                     f"{_f(_mean([r['r'] for r in tp]))}; passes that hit stop first {sum(r['outcome'] == 'stop' for r in tp)}, "
                     f"target first {sum(r['outcome'] == 'target' for r in tp)}.")
    lines.append("All days: " + _diff_line(rep["buy_minus_pass"]) + ".")
    c = rep["calibration"]
    if c["ready"]:
        lines.append(f"Your p_win: Brier {_f(c['brier_all'], '.3f')} on n={c['n']}; "
                     + ", ".join(f"{b['bin']}: said {_f(b['mean_p_win'], '.0f')} got {_f(b['target_first_pct'], '.0f')}%"
                                 for b in c["bins"] if b["n"]) + ".")
    lines = lines[:REVIEW_MAX_LINES - 1] + [HINDSIGHT_TAIL]
    room = REVIEW_MAX_CHARS - len(HINDSIGHT_TAIL) - 1
    body = "\n".join(lines[:-1])
    if len(body) > room:
        body = body[:room - 3].rstrip() + "..."
    return body + "\n" + HINDSIGHT_TAIL


# ---------------------------------------------------------------- CLI
def write_report(folder: str = HERE) -> tuple[dict, str]:
    rep = report(folder)
    rep["updated_t"] = datetime.now(ET).timestamp()
    txt = report_text(rep)
    lab._write_text(os.path.join(folder, "data", "decision_score.txt"), txt)
    write_json(os.path.join(folder, "data", "decision_score.json"), rep, indent=1)
    return rep, txt


def main(folder: str = HERE) -> None:
    _rep, txt = write_report(folder)
    print(txt)
    print("Saved data/decision_score.txt and data/decision_score.json")


if __name__ == "__main__":
    main()
