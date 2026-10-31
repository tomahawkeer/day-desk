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
            **summarize(rows), "skipped": skipped, "rows": rows}


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
