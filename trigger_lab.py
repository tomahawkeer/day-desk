"""Trigger lab: which scanner triggers are worth a Claude call? Measured from saved 1-minute bars, ZERO Claude calls.

Run TRIGGER_LAB.bat (or: python trigger_lab.py). It reads data/bars/<day>.json and data/decisions.jsonl,
prints a plain-English table and saves data/trigger_lab.txt and data/trigger_lab.json.

What it does, in plain English:
  1. REPLAY. For every saved day it walks the day minute by minute exactly like the live loop: each tick the real
     scanner.scan() looks at each stock's COMPLETED 1-minute bars (plus QQQ's, lined up by time), the real
     scanner.Cooldowns decides whether the desk would have asked, and the live loop's limit of 2 questions per
     tick is applied. Every question it would have asked is a "fire".
  2. STANDARD SHADOW TRADE. Every fire gets the same pretend trade (a fixed yardstick, not a strategy):
     buy at the open of the next bar, stop 2 typical 1-minute ranges below (never more than max_stop_pct),
     target 1.5x the stop distance above, engine fill rules, out at 3:45 if nothing hits. Result in R
     (1R = the money at risk), after costs. Also how the stock did vs QQQ 15/30/60 minutes later.
  3. RANDOM BASELINE. For each fire, 20 random moments on the same stock and day get the same pretend trade.
     Lift = how much better the trigger's moments were than random ones. Lift is the number that matters:
     a trigger with lift near zero is just waking Claude at random moments.
  4. NOISE CHECK. A bootstrap that reshuffles WHOLE DAYS (2,000 times) gives a 90% range for the lift.
     If that range includes 0, the honest answer is "within noise".

Everything here is EXPLORATORY: a handful of days. Changing a trigger because of it needs a written note first,
then a confirm run on LATER days only:  python trigger_lab.py --confirm-after <last day used>
(a confirm run can be done once per date; it refuses a second go so nobody keeps re-rolling until it says yes).

Rulings (made 10-31, when this was built):
  - Fire time = the live tick time. Tick 1 of a minute is the moment the previous bar completes; tick 2 is
    poll_seconds later. A fire at tick 1 buys at the open of the bar starting right then; at tick 2, at the open of
    the next one ("the next bar that opens after the fire").
  - Position reviews, the daily-loss circuit breaker and the daily call cap are not replayed (they depend on the
    desk's own trades). The 2-per-tick limit is applied to scanner fires only, symbols in the order given.
  - The shadow trade walks bars AFTER the entry bar (like the engine, which only checks bars that open after the
    buy). Fires whose entry bar opens at or after last_buy_minute (3:30) still count as calls but are not labelled:
    the engine would refuse that buy.
  - Forward returns are entry-bar open -> close of the bar ending h minutes later, minus QQQ's over the same window;
    empty if the day ends first.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from datetime import date, datetime

from engine import COST_PER_SIDE, ET, hm, minutes_et
from health import write_json
from lab import walk_exits
from scanner import Cooldowns, scan

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- the yardstick. These resemble the desk's own median stop (~1.8 typical ranges) and the prompt's 1.5x reward
# ---- rule; NOT tuned for profit. Never change them to make a trigger look better.
STOP_TYPICAL_MULT = 2.0        # stop = entry x (1 - 2 x typical 1-minute range), clamped to config max_stop_pct
REWARD_MULT = 1.5              # target = entry + 1.5 x (entry - stop)
FWD_MINUTES = (15, 30, 60)     # forward returns vs QQQ
RANDOM_PER_FIRE = 20           # random moments per fire, same stock and day
RANDOM_SEED = 20261031         # fixed seeds: same files in, same answer out
BOOT_N = 2000                  # bootstrap resamples (whole days)
BOOT_SEED = 1031
CI_PCT = 90                    # width of the bootstrap range

# ---- the live loop (read from day_desk.Desk.step() on 10-31)
MAX_ASKS_PER_TICK = 2          # step(): `if len(seen) > 2: break` -> at most 2 distinct stocks asked per tick
MARKET_OPEN_MINUTE = 9 * 60 + 30
PARITY_SECONDS = 120           # a replayed fire matches a live trigger if same stock + kind and within 2 minutes
SCANNER_KINDS = ("momentum", "flush_bounce", "vwap_reclaim", "breakout")
LIVE_COMPLETED_FROM = "2026-10-31"   # before this day the live loop scanned the still-forming bar


# ---------------------------------------------------------------- loading
def load_config(folder: str) -> dict:
    path = os.path.join(folder, "config.json")
    if not os.path.exists(path):
        path = os.path.join(HERE, "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def archive_days(folder: str) -> list[str]:
    d = os.path.join(folder, "data", "bars")
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json") and len(f) == 15)


def load_day(folder: str, day: str) -> dict:
    try:
        with open(os.path.join(folder, "data", "bars", day + ".json"), encoding="utf-8") as f:
            bars = json.load(f).get("bars", {}) or {}
    except (OSError, ValueError):
        return {}
    return {s: sorted(b, key=lambda x: x["t"]) for s, b in bars.items() if b}


def load_watchlist(folder: str) -> list[str]:
    try:
        with open(os.path.join(folder, "data", "watchlist.json"), encoding="utf-8") as f:
            return list(json.load(f).get("symbols", []))
    except (OSError, ValueError):
        return []


def load_live(folder: str) -> list[dict]:
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
            if "t" in r:
                r["day"] = datetime.fromtimestamp(float(r["t"]), ET).date().isoformat()
                out.append(r)
    return out


def minute_ts(day: str, minute: int) -> float:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, minute // 60, minute % 60, tzinfo=ET).timestamp()


def day_of_bars(day_bars: dict) -> str | None:
    for b in day_bars.values():
        if b:
            return datetime.fromtimestamp(b[0]["t"], ET).date().isoformat()
    return None


# ---------------------------------------------------------------- 1) replay
def replay_day(day_bars: dict, cfg: dict, symbols: list[str], stats: dict | None = None) -> list[tuple]:
    """Walk one day like the live loop and return the fires: (sym, kind, t, reason), t = live tick time.

    Each tick sees only COMPLETED bars (bars that opened before the current minute). Cooldowns are the real
    scanner.Cooldowns (in memory). Per tick: each stock offers its first trigger whose cooldown is ready; at most
    MAX_ASKS_PER_TICK stocks are asked; only asked ones are marked. stats (optional dict) gets 'crowded_out'."""
    day = day_of_bars(day_bars)
    if day is None:
        return []
    bars = {s: sorted(day_bars.get(s) or [], key=lambda b: b["t"]) for s in set(symbols) | {"QQQ"}}
    cool = Cooldowns(cfg, path=None)
    poll = max(1, int(cfg.get("poll_seconds", 30)))
    ticks = max(1, 60 // poll)
    ptr = {s: 0 for s in bars}
    fires: list[tuple] = []
    crowded = 0
    for minute in range(MARKET_OPEN_MINUTE + 1, int(cfg["flatten_minute"])):   # asks stop at the 3:45 close-out
        t0 = minute_ts(day, minute)
        for s, b in bars.items():                   # completed bars = opened before this minute
            i = ptr[s]
            while i < len(b) and b[i]["t"] < t0:
                i += 1
            ptr[s] = i
        qqq = bars["QQQ"][:ptr["QQQ"]]
        if not qqq:                                  # live: no QQQ data -> the tick does nothing
            continue
        found = {s: scan(s, bars[s][:ptr[s]], qqq, cfg) for s in symbols}   # same bars both ticks -> scan once
        for k in range(ticks):
            now = t0 + k * poll
            asks = []
            for s in symbols:
                for kind, reason in found[s]:
                    if cool.ready(s, kind, now):
                        asks.append((s, kind, reason))
                        break
            seen = set()
            for s, kind, reason in asks:
                if s in seen:
                    continue
                seen.add(s)
                if len(seen) > MAX_ASKS_PER_TICK:
                    crowded += len({a[0] for a in asks}) - MAX_ASKS_PER_TICK
                    break
                cool.mark(s, kind, now)
                fires.append((s, kind, now, reason))
    if stats is not None:
        stats["crowded_out"] = stats.get("crowded_out", 0) + crowded
    return fires


# ---------------------------------------------------------------- 2) the standard shadow trade
def _close_by(bars: list[dict], end_t: float) -> float | None:
    """Close of the last bar that ENDED by end_t; None if the day's bars stop before end_t."""
    if not bars or bars[-1]["t"] + 60 < end_t:
        return None
    px = None
    for b in bars:
        if b["t"] + 60 <= end_t:
            px = b["c"]
        else:
            break
    return px


def shadow_trade(sym_bars: list[dict], qqq_bars: list[dict], fire_t: float, cfg: dict) -> dict | None:
    """The fixed yardstick trade for a decision at fire_t. None if it can't be placed (no bar, after last buy)."""
    j = next((i for i, b in enumerate(sym_bars) if b["t"] >= fire_t), None)
    if j is None:
        return None
    eb = sym_bars[j]
    if minutes_et(eb["t"]) >= cfg["last_buy_minute"]:
        return None
    morning = [b for b in sym_bars[:j] if minutes_et(b["t"]) >= MARKET_OPEN_MINUTE and b["o"] > 0]
    if not morning:
        return None
    typical = statistics.fmean((b["h"] - b["l"]) / b["o"] for b in morning)
    dist = min(STOP_TYPICAL_MULT * typical, float(cfg["max_stop_pct"]) / 100)
    entry = eb["o"]
    if dist <= 0 or entry <= 0:
        return None
    stop = entry * (1 - dist)
    target = entry + REWARD_MULT * (entry - stop)
    flat = [b for b in sym_bars if minutes_et(b["t"]) < cfg["flatten_minute"]]
    hit = walk_exits(flat, stop, target, eb["t"])
    if hit:
        px, why, xt = hit["price"], hit["reason"], hit["t"]
    else:
        later = [b for b in flat if b["t"] > eb["t"]] or [eb]
        px, why, xt = later[-1]["c"], "close-out", later[-1]["t"] + 60
    r = (px * (1 - COST_PER_SIDE) - entry * (1 + COST_PER_SIDE)) / (entry - stop)
    out = {"entry_t": eb["t"], "entry": entry, "stop": stop, "target": target, "typical": typical,
           "exit": px, "exit_t": xt, "exit_reason": why, "r": r, "target_first": why == "target"}
    q0 = next((b["o"] for b in qqq_bars if b["t"] >= eb["t"]), None)
    for h in FWD_MINUTES:
        end = eb["t"] + h * 60
        p1, q1 = _close_by(sym_bars, end), _close_by(qqq_bars, end)
        out[f"fwd{h}"] = ((p1 / entry - 1) - (q1 / q0 - 1)) * 100 if (p1 and q1 and q0) else None
    return out


# ---------------------------------------------------------------- 3) random baseline
def random_minutes(day: str, sym: str, kind: str, t: float, cfg: dict, n: int = RANDOM_PER_FIRE) -> list[int]:
    """n random decision minutes in [scanner start_minute, last_buy_minute), seeded by the fire itself."""
    rng = random.Random(f"{RANDOM_SEED}|{day}|{sym}|{kind}|{int(t)}")
    lo, hi = int(cfg["scanner"]["start_minute"]), int(cfg["last_buy_minute"])
    return [rng.randrange(lo, hi) for _ in range(n)]


def random_labels(day: str, sym: str, kind: str, t: float, sym_bars, qqq_bars, cfg) -> list[dict]:
    out = []
    for m in random_minutes(day, sym, kind, t, cfg):
        lab = shadow_trade(sym_bars, qqq_bars, minute_ts(day, m), cfg)
        if lab:
            out.append(lab)
    return out


# ---------------------------------------------------------------- 4) statistics
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (statistics.fmean(xs), len(xs)) if xs else (None, 0)


def day_bootstrap(fire_r: dict, rand_r: dict, n: int = BOOT_N, seed: int = BOOT_SEED, pct: int = CI_PCT):
    """Bootstrap of lift (mean fire R - mean random R) resampling WHOLE DAYS. fire_r/rand_r: {day: [R, ...]}.
    Returns (low, high, resamples used) or (None, None, 0) when there is nothing to resample."""
    days = sorted(set(fire_r) | set(rand_r))
    if not days:
        return None, None, 0
    rng = random.Random(seed)
    lifts = []
    for _ in range(n):
        pick = [rng.choice(days) for _ in days]
        f = [x for d in pick for x in fire_r.get(d, [])]
        r = [x for d in pick for x in rand_r.get(d, [])]
        if f and r:
            lifts.append(statistics.fmean(f) - statistics.fmean(r))
    if not lifts:
        return None, None, 0
    lifts.sort()
    tail = (100 - pct) / 200
    lo = lifts[int(tail * (len(lifts) - 1))]
    hi = lifts[int(round((1 - tail) * (len(lifts) - 1)))]
    return lo, hi, len(lifts)


# ---------------------------------------------------------------- the report
def pick_symbols(day_bars: dict, mode: str, watchlist: list[str]) -> list[str]:
    if mode == "watchlist":
        return [s for s in watchlist if s in day_bars and s != "QQQ"]
    return sorted(s for s in day_bars if s != "QQQ")


def analyse(folder: str, days: list[str], mode: str = "all", cfg: dict | None = None) -> dict:
    cfg = cfg or load_config(folder)
    wl = load_watchlist(folder)
    fires, crowded = [], 0
    for day in days:
        db = load_day(folder, day)
        if not db.get("QQQ"):
            continue
        syms = pick_symbols(db, mode, wl)
        st: dict = {}
        for sym, kind, t, reason in replay_day(db, cfg, syms, st):
            crowded += st.pop("crowded_out", 0)
            rec = {"day": day, "sym": sym, "kind": kind, "t": t, "time": hm(t), "reason": reason}
            lab = shadow_trade(db[sym], db["QQQ"], t, cfg)
            rec["label"] = lab
            rec["random_r"] = [x["r"] for x in random_labels(day, sym, kind, t, db[sym], db["QQQ"], cfg)] if lab else []
            fires.append(rec)
        crowded += st.pop("crowded_out", 0)
    used_days = sorted({d for d in days if load_day(folder, d).get("QQQ")})
    rows = []
    for kind in list(SCANNER_KINDS) + ["ALL"]:
        fs = [f for f in fires if kind == "ALL" or f["kind"] == kind]
        lab = [f for f in fs if f["label"]]
        mr, n_r = _mean([f["label"]["r"] for f in lab])
        tf = sum(1 for f in lab if f["label"]["target_first"])
        f30, n30 = _mean([f["label"]["fwd30"] for f in lab])
        rr, n_rr = _mean([x for f in lab for x in f["random_r"]])
        fire_by_day, rand_by_day = {}, {}
        for f in lab:
            fire_by_day.setdefault(f["day"], []).append(f["label"]["r"])
            rand_by_day.setdefault(f["day"], []).extend(f["random_r"])
        lo, hi, nb = day_bootstrap(fire_by_day, rand_by_day)
        lift = mr - rr if (mr is not None and rr is not None) else None
        verdict = "no data" if lo is None else ("within noise" if lo <= 0 <= hi else
                                                ("above random (exploratory)" if lo > 0 else "below random (exploratory)"))
        rows.append({"trigger": kind, "fires": len(fs), "days": len(used_days),
                     "per_day": len(fs) / len(used_days) if used_days else None,
                     "n_labelled": len(lab), "mean_r": mr, "n_r": n_r,
                     "target_first_pct": 100 * tf / len(lab) if lab else None,
                     "fwd30_vs_qqq": f30, "n_fwd30": n30, "random_mean_r": rr, "n_random": n_rr,
                     "lift": lift, "ci_low": lo, "ci_high": hi, "n_boot": nb, "n_fire_days": len(fire_by_day),
                     "verdict": verdict})
    return {"days": used_days, "symbols_mode": mode, "fires": fires, "rows": rows, "crowded_out": crowded,
            "yardstick": {"stop_typical_mult": STOP_TYPICAL_MULT, "reward_mult": REWARD_MULT,
                          "max_stop_pct": cfg["max_stop_pct"], "cost_per_side": COST_PER_SIDE,
                          "random_per_fire": RANDOM_PER_FIRE, "boot_n": BOOT_N, "ci_pct": CI_PCT}}


# ---------------------------------------------------------------- 6) parity with the live log
def parity(folder: str, days: list[str], cfg: dict | None = None) -> list[dict]:
    """Replay each day that has live scanner triggers on the stocks the desk was watching that day (today's watchlist
    plus every stock in that day's log) and match against the logged triggers: same stock, same kind, within 2 min."""
    cfg = cfg or load_config(folder)
    live = [r for r in load_live(folder) if r.get("trigger") in SCANNER_KINDS]
    wl = load_watchlist(folder)
    out = []
    for day in days:
        lv = sorted((r for r in live if r["day"] == day), key=lambda r: r["t"])
        if not lv:
            continue
        db = load_day(folder, day)
        logged = {r.get("sym") for r in load_live(folder) if r["day"] == day} - {None, "-", ""}
        syms = [s for s in wl if s in db] + sorted(s for s in logged if s in db and s not in wl)
        fires = replay_day(db, cfg, syms) if db.get("QQQ") else []
        used, misses = set(), []
        for r in lv:
            best = None
            for i, (s, k, t, _) in enumerate(fires):
                if i in used or s != r["sym"] or k != r["trigger"] or abs(t - float(r["t"])) > PARITY_SECONDS:
                    continue
                if best is None or abs(t - float(r["t"])) < abs(fires[best][2] - float(r["t"])):
                    best = i
            if best is None:
                misses.append(f"{r['sym']} {r['trigger']} {hm(float(r['t']))}")
            else:
                used.add(best)
        out.append({"day": day, "live": len(lv), "matched": len(lv) - len(misses), "replayed": len(fires),
                    "replay_only": len(fires) - len(used), "misses": misses, "symbols": syms,
                    "forming_bar_era": day < LIVE_COMPLETED_FROM})
    return out


# ---------------------------------------------------------------- text
def _f(x, fmt="{:+.2f}", none="-"):
    return none if x is None else fmt.format(x)


def report_text(rep: dict, par: list[dict], confirm_after: str | None = None) -> str:
    n = len(rep["days"])
    span = f"{rep['days'][0]} .. {rep['days'][-1]}" if n else "none"
    L = ["DAY DESK TRIGGER LAB  (zero Claude calls; replays the real scanner on saved 1-minute bars)", ""]
    if confirm_after:
        L.append(f"CONFIRM RUN: only days after {confirm_after} (n={n} days: {span}). This run can be done once.")
    else:
        L.append(f"!! EXPLORATORY on n={n} days ({span}). Changing a trigger needs a written note first, then a confirm")
        L.append(f"!! run on LATER days only:  python trigger_lab.py --confirm-after {rep['days'][-1] if n else 'YYYY-MM-DD'}")
    y = rep["yardstick"]
    L += ["", f"Yardstick (fixed, not tuned): buy at next bar's open, stop {y['stop_typical_mult']:g} typical 1-min ranges "
              f"below (max {y['max_stop_pct']:g}%), target {y['reward_mult']:g}x the risk, out at 3:45. R = result in units "
              f"of the risk, after {y['cost_per_side'] * 100:.2f}%/side.",
          f"Random = {y['random_per_fire']} random moments per fire, same stock and day. Lift = trigger R - random R. "
          f"CI = {y['ci_pct']}% bootstrap over whole days ({y['boot_n']} resamples).",
          f"Stocks: {rep['symbols_mode']} in the archive except QQQ. Fires crowded out by the 2-per-tick limit: "
          f"{rep['crowded_out']}.", ""]
    hdr = (f"{'trigger':<13}{'fires':>12}{'/day':>7}  {'mean R':>14}  {'target 1st':>14}  {'fwd30 vs QQQ':>16}  "
           f"{'random R':>16}  {'lift':>7}  {'90% CI of lift':>18}  verdict")
    L += [hdr, "-" * len(hdr)]
    for r in rep["rows"]:
        L.append(f"{r['trigger']:<13}{r['fires']:>5} (n={r['days']}d){_f(r['per_day'], '{:.1f}'):>6}  "
                 f"{_f(r['mean_r']):>6} (n={r['n_r']:<4})  {_f(r['target_first_pct'], '{:.0f}%'):>5} (n={r['n_labelled']:<4})  "
                 f"{_f(r['fwd30_vs_qqq'], '{:+.2f}%'):>7} (n={r['n_fwd30']:<4})  "
                 f"{_f(r['random_mean_r']):>6} (n={r['n_random']:<5})  {_f(r['lift']):>7}  "
                 f"[{_f(r['ci_low'])}, {_f(r['ci_high'])}] (n={r['n_fire_days']}d)  {r['verdict']}")
    L += ["", "fires/day = the most Claude calls per day this trigger costs (before cooldown overlaps with live trades).",
          "Fires after 3:30 count as calls but get no shadow trade (the engine refuses buys then), so n can be < fires."]
    if par:
        L += ["", "PARITY with the live log (same stock + trigger, within 2 min; replayed on that day's watched stocks):"]
        for p in par:
            rate = 100 * p["matched"] / p["live"] if p["live"] else 0
            L.append(f"  {p['day']}: matched {p['matched']} of n={p['live']} live triggers ({rate:.0f}%); "
                     f"replay fired n={p['replayed']}, {p['replay_only']} not in the live log"
                     + (f"; misses: {', '.join(p['misses'])}" if p["misses"] else ""))
        if any(p["forming_bar_era"] for p in par):
            L.append("  Note: until 10-31 the live loop scanned the still-forming bar (and Claude calls block the loop),")
            L.append("  so some mismatch is expected for 10-28..10-30. The replay uses completed bars only.")
    L += ["", f"Exploratory, n={n} days — a clue, not proof."]
    return "\n".join(L) + "\n"


def summary_lines(folder: str = HERE) -> str:
    """At most 6 lines for the end-of-day review prompt. Uses data/trigger_lab.json when it covers every saved day
    (all-stocks mode); otherwise rebuilds it."""
    days = archive_days(folder)
    rep = None
    try:
        with open(os.path.join(folder, "data", "trigger_lab.json"), encoding="utf-8") as f:
            cached = json.load(f)
        missing = [d for d in days if d not in set(cached.get("days", []))]
        if cached.get("symbols_mode") == "all" and not cached.get("confirm_after") and cached.get("rows") \
                and not any(load_day(folder, d).get("QQQ") for d in missing):
            rep = cached
    except (OSError, ValueError):
        pass
    if rep is None:
        rep, _ = write_report(folder)
    n = len(rep["days"])
    rows = {r["trigger"]: r for r in rep["rows"]}
    a = rows["ALL"]
    L = [f"Trigger lab (standard shadow trade, n={n} days, n={a['fires']} replayed fires): R vs 20 random moments, same stock/day."]
    for k in SCANNER_KINDS:
        r = rows[k]
        if not r["fires"]:
            L.append(f"{k}: no fires (n=0)")
            continue
        L.append(f"{k}: {r['fires']} fires ({_f(r['per_day'], '{:.1f}')}/day), mean R {_f(r['mean_r'])} (n={r['n_r']}) "
                 f"vs random {_f(r['random_mean_r'])} (n={r['n_random']}), lift {_f(r['lift'])} "
                 f"[{_f(r['ci_low'])}, {_f(r['ci_high'])}] -> {r['verdict']}")
    L.append(f"Exploratory, n={n} days — a clue, not proof.")
    return "\n".join(L[:6])


# ---------------------------------------------------------------- saving / CLI
def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _jsonable(rep: dict) -> dict:
    out = dict(rep)
    out["fires"] = [{**f, "random_r": [round(x, 4) for x in f["random_r"]]} for f in rep["fires"]]
    return out


def write_report(folder: str = HERE, mode: str = "all", confirm_after: str | None = None) -> tuple[dict, str]:
    cfg = load_config(folder)
    days = archive_days(folder)
    if confirm_after:
        days = [d for d in days if d > confirm_after]
    rep = analyse(folder, days, mode, cfg)
    par = parity(folder, rep["days"], cfg)
    rep["parity"] = par
    rep["confirm_after"] = confirm_after
    rep["updated_t"] = datetime.now(ET).timestamp()
    txt = report_text(rep, par, confirm_after)
    stem = f"trigger_lab_confirm_{confirm_after}" if confirm_after else "trigger_lab"
    _write_text(os.path.join(folder, "data", stem + ".txt"), txt)
    write_json(os.path.join(folder, "data", stem + ".json"), _jsonable(rep), indent=1)
    return rep, txt


def stamp_path(folder: str, confirm_after: str) -> str:
    return os.path.join(folder, "data", f"trigger_lab_confirm_{confirm_after}.stamp")


def main(argv: list[str] | None = None, folder: str = HERE) -> int:
    ap = argparse.ArgumentParser(description="Which scanner triggers are worth a Claude call? (zero Claude calls)")
    ap.add_argument("--confirm-after", metavar="YYYY-MM-DD", help="confirm run: only days after this date, once")
    ap.add_argument("--symbols", choices=["all", "watchlist"], default="all",
                    help="all = every stock in the archive except QQQ (default); watchlist = data/watchlist.json")
    a = ap.parse_args(argv)
    ca = a.confirm_after
    if ca:
        try:
            date.fromisoformat(ca)
        except ValueError:
            print(f"--confirm-after needs a date like 2026-10-30, not '{ca}'.")
            return 2
        sp = stamp_path(folder, ca)
        if os.path.exists(sp):
            print(f"REFUSED: the confirm run after {ca} was already done (see {os.path.relpath(sp, folder)} and "
                  f"data/trigger_lab_confirm_{ca}.txt).")
            print("A confirm run counts once. Running it again until it says yes would turn it back into a fishing trip.")
            return 1
        if not [d for d in archive_days(folder) if d > ca]:
            print(f"No saved days after {ca} yet. Nothing to confirm; no stamp written. Try again after more trading days.")
            return 1
    rep, txt = write_report(folder, a.symbols, ca)
    print(txt)
    if ca:
        write_json(stamp_path(folder, ca), {"confirm_after": ca, "days": rep["days"], "done_t": rep["updated_t"],
                                             "rows": rep["rows"]}, indent=1)
        print(f"Saved data/trigger_lab_confirm_{ca}.txt/.json and the stamp (this confirm can't be re-run).")
    else:
        print("Saved data/trigger_lab.txt and data/trigger_lab.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
