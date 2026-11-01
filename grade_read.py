"""Day Desk pre-registered grade (Read 1 at 60 closed trades, Read 2 at 120).

Run:  python grade_read.py            (or double-click GRADE.bat)
      python grade_read.py --selftest (proves the maths on made-up data; never reads the real trades)

WHAT THIS DOES, IN PLAIN ENGLISH
- Below 60 closed trades it prints PROGRESS ONLY: how many trades so far, how fast they are coming,
  and whether the plumbing (saved bars, known stops) is healthy. It deliberately prints no verdict.
- At 60 it grades the FIRST 60 trades by exit time (Read 1); at 120 the first 120 (Read 2).
- G1 "Profitable": total net P&L of the graded trades is above $0 at the engine cost (0.01% per side).
- G2 "Beats random timing": 2,000 copies of the whole record. In each copy every real trade is replayed on the
  same stock, same day, same share count, same holding minutes, but with a random entry minute. G2 passes if
  the real total beats at least 95% of the 2,000 copy totals.
- PASS = G1 AND G2, always reported as "x of 2".

RULINGS ON THE PRE-REGISTRATION WORDING (written here so nobody has to guess later)
1. Entry window. The text says entries run from 9:45 to "the later of 15:30 and 15:45 minus the hold, so every
   copy closes by 3:45". "The later of" contradicts "every copy closes by 3:45" (a 5-minute hold could then
   enter at 15:40 and close at 15:45, but a 60-minute hold entering at 15:30 would close at 16:30). Closing by
   3:45 is the binding requirement, so the latest entry minute is min(15:30, 15:45 - hold). If that leaves no
   room (hold longer than 6 hours) the only entry minute is 9:45.
2. Holding minutes = (exit time - entry time) in minutes, rounded to the nearest whole minute, at least 1.
3. A copy buys at the close of the 1-minute bar that OPENS at the drawn minute and sells at the close of the bar
   that opens `hold` minutes later. If a bar is missing, the last close before it is used.
4. Trades whose day has no saved bars (or whose stock is missing from that day's file) are counted in G1 but
   left out of the copies. G2 then compares like with like: the real P&L of the trades that DO have copies
   against the copy totals. If more than 10% of graded trades are left out, G2 is UNKNOWN.
5. "Beats" means strictly greater than. The percentile reported is the share of copies whose total is strictly
   below the real total.
6. 95th percentile of the copies = nearest-rank: the value at position ceil(0.95 x 2000) in the sorted list.
   Resolution floor = that 95th percentile minus the copies' median.
7. Stop-applied copies (reported, not graded): the same 2,000 copies, but each trade carries its real stop
   DISTANCE (buy price before cost minus stop) below the copy's entry price as a resting stop, using the same
   fill rule as engine.check_exits (fills at the stop, or at the bar's open if the price gapped through it).
   No target is applied. Trades whose stop is unknown are replayed without a stop (count reported).
8. Trades per trading day uses every ET day on which the desk logged anything (decisions or trades), so quiet
   days count. Self-test records are ignored everywhere.
9. Research: graded = vs_qqq_pct present. The read uses the FIRST 40 graded high-conviction picks in file order.
   "Medium/low on the same days" = medium/low graded picks on the days those 40 high picks came from.
   If there are no such medium/low picks, the comparison cannot be won, so no edge is claimed.
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from engine import COST_PER_SIDE

ET = ZoneInfo("America/New_York")
SEED = 20261102
N_COPIES = 2000
N_BOOT = 10000
READS = [(60, "Read 1"), (120, "Read 2")]
RESEARCH_NEEDED = 40
STRESS_COST = 0.0005
FIRST_ENTRY = 9 * 60 + 45      # 9:45
LAST_ENTRY_CAP = 15 * 60 + 30  # 15:30
CLOSE_OUT = 15 * 60 + 45       # 15:45
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- loading
def et_day(t: float) -> str:
    return datetime.fromtimestamp(t, ET).date().isoformat()


def et_minute(t: float) -> int:
    d = datetime.fromtimestamp(t, ET)
    return d.hour * 60 + d.minute


def load_trades(folder: str) -> list[dict]:
    path = os.path.join(folder, "data", "trades.csv")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"sym": r["sym"], "shares": int(float(r["shares"])), "entry": float(r["entry"]),
                        "exit": float(r["exit"]), "entry_t": float(r["entry_t"]), "exit_t": float(r["exit_t"]),
                        "pnl": float(r["pnl"]), "setup": (r.get("setup") or "").strip() or "untagged"})
    out.sort(key=lambda x: x["exit_t"])
    return out


# Rows the ENGINE writes to decisions.jsonl, not Claude calls (same list as day_desk.NOT_CLAUDE_CALLS; a test
# keeps the two identical).
NOT_CLAUDE_CALLS = {"resting order", "3:45 PM close-out", "new day", "research grading", "re-entry lock"}


def load_decisions(folder: str) -> list[dict]:
    path = os.path.join(folder, "data", "decisions.jsonl")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("trigger") == "self-test":
                    continue
                out.append(d)
    return out


def attach_stops(trades: list[dict], decisions: list[dict]) -> int:
    """Give each trade its stop distance from the matching buy record. Returns how many stops are known."""
    buys = [d for d in decisions if d.get("action") == "buy" and str(d.get("result", "")).startswith("bought")]
    known = 0
    for tr in trades:
        tr["stop_dist"] = None
        for d in buys:
            if d.get("sym") == tr["sym"] and abs(float(d.get("t", 0)) - tr["entry_t"]) < 5:
                try:
                    dist = float(d["price"]) - float(d["stop"])
                except (KeyError, TypeError, ValueError):
                    break
                if dist > 0:
                    tr["stop_dist"] = dist
                    known += 1
                break
    return known


class Bars:
    """Saved 1-minute bars, loaded per day on demand. Index: day -> sym -> {ET minute: bar}."""

    def __init__(self, folder: str):
        self.dir = os.path.join(folder, "data", "bars")
        self.cache: dict[str, dict | None] = {}

    def day(self, day: str) -> dict | None:
        if day not in self.cache:
            path = os.path.join(self.dir, day + ".json")
            if not os.path.exists(path):
                self.cache[day] = None
            else:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f).get("bars", {})
                self.cache[day] = {s: {et_minute(b["t"]): b for b in bs} for s, bs in raw.items() if bs}
        return self.cache[day]

    def sym(self, day: str, sym: str) -> dict | None:
        d = self.day(day)
        return d.get(sym) if d else None


def bar_at(by_min: dict, m: int) -> dict:
    """Bar that opens at minute m; else the last bar before it; else the first bar after it."""
    if m in by_min:
        return by_min[m]
    before = [k for k in by_min if k <= m]
    return by_min[max(before)] if before else by_min[min(by_min)]


# ---------------------------------------------------------------- the copies
def hold_minutes(tr: dict) -> int:
    return max(1, round((tr["exit_t"] - tr["entry_t"]) / 60))


def entry_window(hold: int) -> tuple[int, int]:
    """Ruling 1: entries run 9:45 .. min(15:30, 15:45 - hold); if empty, 9:45 only."""
    last = min(LAST_ENTRY_CAP, CLOSE_OUT - hold)
    if last < FIRST_ENTRY:
        last = FIRST_ENTRY
    return FIRST_ENTRY, last


def copy_pnl(by_min: dict, m: int, hold: int, shares: int, stop_dist: float | None = None) -> float:
    """P&L of one copied trade entered at the close of minute m and held `hold` minutes.
    With stop_dist, a resting stop sits stop_dist below the raw entry price (engine.check_exits fill rule)."""
    raw_in = bar_at(by_min, m)["c"]
    raw_out = None
    if stop_dist is not None:
        stop = raw_in - stop_dist
        for k in range(m + 1, m + hold + 1):
            b = by_min.get(k)
            if b is None:
                continue
            if b["l"] <= stop:
                raw_out = min(stop, b["o"])
                break
    if raw_out is None:
        raw_out = bar_at(by_min, m + hold)["c"]
    return shares * (raw_out * (1 - COST_PER_SIDE) - raw_in * (1 + COST_PER_SIDE))


def run_copies(trades: list[dict], bars: Bars, seed: int = SEED, n: int = N_COPIES) -> dict:
    """Plain copies (G2) and stop-applied copies (reported only), sharing the same random entry minutes."""
    usable, left_out = [], []
    for tr in trades:
        bm = bars.sym(et_day(tr["entry_t"]), tr["sym"])
        (usable if bm else left_out).append((tr, bm))
    rng = random.Random(seed)
    plain, stopped = [], []
    for _ in range(n):
        a = s = 0.0
        for tr, bm in usable:
            h = hold_minutes(tr)
            lo, hi = entry_window(h)
            m = rng.randint(lo, hi)
            a += copy_pnl(bm, m, h, tr["shares"])
            s += copy_pnl(bm, m, h, tr["shares"], tr.get("stop_dist"))
        plain.append(a)
        stopped.append(s)
    return {"plain": plain, "stopped": stopped, "n_usable": len(usable), "n_left_out": len(left_out),
            "real_usable": sum(tr["pnl"] for tr, _ in usable),
            "stops_unknown_in_copies": sum(1 for tr, _ in usable if tr.get("stop_dist") is None)}


def pct_below(values: list[float], x: float) -> float:
    return sum(1 for v in values if v < x) / len(values) if values else 0.0


def p95(values: list[float]) -> float:
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def floor_of(values: list[float]) -> float:
    return p95(values) - statistics.median(values)


def grade(trades: list[dict], bars: Bars, seed: int = SEED, n: int = N_COPIES) -> dict:
    total = round(sum(t["pnl"] for t in trades), 2)
    g1 = total > 0
    c = run_copies(trades, bars, seed, n)
    frac_out = c["n_left_out"] / len(trades) if trades else 1.0
    if c["n_usable"] == 0 or frac_out > 0.10:
        g2 = None
        pct = None
    else:
        pct = pct_below(c["plain"], c["real_usable"])
        g2 = pct >= 0.95
    res = dict(c)
    res.update({"total": total, "g1": g1, "g2": g2, "pct": pct,
                "passed": int(g1) + int(g2 is True),
                "verdict": "UNKNOWN" if g2 is None else ("PASS" if (g1 and g2) else "FAIL"),
                "floor": floor_of(c["plain"]) if c["plain"] and c["n_usable"] else None,
                "p95": p95(c["plain"]) if c["n_usable"] else None,
                "median": statistics.median(c["plain"]) if c["n_usable"] else None,
                "stop_pct": pct_below(c["stopped"], c["real_usable"]) if c["n_usable"] else None})
    return res


# ---------------------------------------------------------------- reported, not graded
def stress_pnl(tr: dict, cost: float = STRESS_COST) -> float:
    raw_in = tr["entry"] / (1 + COST_PER_SIDE)
    raw_out = tr["exit"] / (1 - COST_PER_SIDE)
    return tr["shares"] * (raw_out * (1 - cost) - raw_in * (1 + cost))


def max_drawdown(trades: list[dict]) -> float:
    cum = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: x["exit_t"]):
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def desk_days(trades: list[dict], decisions: list[dict]) -> set[str]:
    return {et_day(d["t"]) for d in decisions if "t" in d} | {et_day(t["exit_t"]) for t in trades}


def qqq_days(days: list[str], bars: Bars) -> tuple[float, list[str]]:
    tot, missing = 0.0, []
    for d in days:
        bm = bars.sym(d, "QQQ")
        if not bm:
            missing.append(d)
            continue
        first, last = bm[min(bm)], bm[max(bm)]
        tot += (last["c"] / first["o"] - 1) * 100
    return tot, missing


def research(folder: str) -> dict:
    path = os.path.join(folder, "data", "pick_results.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("vs_qqq_pct"), (int, float)):
                    rows.append(r)
    high = [r for r in rows if r.get("conviction") == "high"]
    out = {"exists": os.path.exists(path), "graded_high": len(high), "ready": len(high) >= RESEARCH_NEEDED}
    if not out["ready"]:
        return out
    high = high[:RESEARCH_NEEDED]
    days = {r["day"] for r in high}
    other = [r["vs_qqq_pct"] for r in rows if r.get("conviction") in ("medium", "low") and r.get("day") in days]
    vals = [r["vs_qqq_pct"] for r in high]
    rng = random.Random(SEED)
    boots = sorted(statistics.fmean(rng.choices(vals, k=len(vals))) for _ in range(N_BOOT))
    lo, hi = boots[int(0.025 * N_BOOT)], boots[int(0.975 * N_BOOT) - 1]
    mean_h = statistics.fmean(vals)
    mean_o = statistics.fmean(other) if other else None
    out.update({"mean_high": mean_h, "lo": lo, "hi": hi, "mean_other": mean_o, "n_other": len(other),
                "edge": lo > 0 and mean_o is not None and mean_h > mean_o})
    return out


# ---------------------------------------------------------------- report text
def money(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def research_lines(r: dict) -> list[str]:
    if not r["ready"]:
        note = "" if r["exists"] else " (no pick_results.jsonl yet; the first rows are written at the 3:45 PM close-out on Mon 2 Nov)"
        return [f"Research picks graded: {r['graded_high']} of {RESEARCH_NEEDED} high-conviction picks{note}.",
                "  (A pick is 'graded' once its move versus QQQ from 9:45 to 3:45 has been recorded.)"]
    L = ["RESEARCH GRADE (separate from the trading verdict; does not change PASS/FAIL)",
         f"  First {RESEARCH_NEEDED} graded high-conviction picks: average {r['mean_high']:+.3f}% versus QQQ "
         f"(9:45 to 3:45).",
         f"  95% range from 10,000 bootstrap resamples: {r['lo']:+.3f}% to {r['hi']:+.3f}%.",
         "  (Bootstrap = re-draw the 40 results at random with replacement 10,000 times to see how much the "
         "average could wobble by luck.)"]
    if r["mean_other"] is None:
        L.append("  Medium/low picks on the same days: none graded, so there is nothing to beat.")
    else:
        L.append(f"  Medium/low picks on the same days: average {r['mean_other']:+.3f}% ({r['n_other']} picks).")
    L.append("  Research has an edge: " + ("YES (range wholly above 0 and high picks beat medium/low)."
                                          if r["edge"] else "NO (needs the range wholly above 0 AND high picks "
                                                            "beating medium/low)."))
    return L


def control_arm_lines(folder: str, end_day: str | None = None, reading: bool = False) -> list[str]:
    """Pre-reg Addendum A4 (control arm), reported only. Never touches PASS/FAIL. In PROGRESS ONLY it prints running
    numbers with no reading; inside a read it covers candidates up to the read's last exit day and adds the reading
    written in A4. A failure here is printed, never allowed to stop the grade."""
    try:
        import decision_score
        ca = decision_score.control_arms(folder, end_day=end_day)
        L = decision_score.a4_lines(ca)
        if reading:
            L.append("  Reading (written in Addendum A4 before any data): " + decision_score.a4_reading(ca))
        return L
    except Exception as e:                       # the grade must still print
        return [f"Control arm (Addendum A4): could not be computed ({e})."]


def read_section(name: str, n_needed: int, graded: list[dict], later: int, bars: Bars,
                 decisions: list[dict], seed: int, n_copies: int, folder: str | None = None) -> list[str]:
    g = grade(graded, bars, seed, n_copies)
    L = [f"===== {name}: first {n_needed} closed trades by exit time =====",
         f"({later} later trade(s) exist and are NOT part of this read.)", ""]
    L.append(f"VERDICT: {g['verdict']} - {g['passed']} of 2 gates met.")
    L.append(f"  G1 Profitable: {'met' if g['g1'] else 'not met'}. Total net P&L {money(g['total'])} "
             f"after the 0.01%-per-side cost.")
    if g["g2"] is None:
        why = ("no trades had saved bars" if g["n_usable"] == 0 else
               f"{g['n_left_out']} of {len(graded)} trades ({100 * g['n_left_out'] / len(graded):.0f}%) have no "
               f"saved bars, more than the 10% allowed")
        L.append(f"  G2 Beats random timing: UNKNOWN ({why}).")
    else:
        L.append(f"  G2 Beats random timing: {'met' if g['g2'] else 'not met'}. The real P&L of the "
                 f"{g['n_usable']} trades with saved bars ({money(g['real_usable'])}) beat "
                 f"{100 * g['pct']:.1f}% of {n_copies:,} random-timing copies (95.0% needed).")
    if g["n_left_out"]:
        L.append(f"  {g['n_left_out']} trade(s) had no saved bars: counted in G1, left out of the copies.")
    if g["floor"] is not None:
        L.append(f"  Resolution floor: {money(g['floor'])} (copies' 95th percentile {money(g['p95'])} minus their "
                 f"median {money(g['median'])}). This is how much better than a typical random copy the record had "
                 "to be just to clear G2; small floors mean the test can see small edges.")
        L.append("  Copies with each trade's real stop distance applied as a resting stop (reported, not graded): "
                 f"real total beat {100 * g['stop_pct']:.1f}% of them"
                 + (f" ({g['stops_unknown_in_copies']} trade(s) with unknown stop replayed without one)."
                    if g["stops_unknown_in_copies"] else "."))
    L += ["", "REPORTED, NOT GRADED"]
    stress = sum(stress_pnl(t) for t in graded)
    L.append(f"  P&L if costs were 0.05% per side instead of 0.01% (a harsher spread): {money(stress)}.")
    wins = [t["pnl"] for t in graded if t["pnl"] > 0]
    losses = [t["pnl"] for t in graded if t["pnl"] <= 0]
    L.append(f"  Win rate: {100 * len(wins) / len(graded):.0f}% ({len(wins)} wins, {len(losses)} losses or flat).")
    aw = statistics.fmean(wins) if wins else 0.0
    al = statistics.fmean(losses) if losses else 0.0
    L.append(f"  Average win {money(aw)}, average loss {money(al)}"
             + (f" (an average win is {aw / abs(al):.2f}x an average loss)." if al else "."))
    L.append(f"  Largest drawdown (biggest drop of running P&L from its high point, in exit order): "
             f"{money(max_drawdown(graded))}.")
    half = len(graded) // 2
    L.append(f"  First half ({half} trades) {money(sum(t['pnl'] for t in graded[:half]))}; "
             f"second half ({len(graded) - half} trades) {money(sum(t['pnl'] for t in graded[half:]))}.")
    for label, key in (("setup tag", "setup"), ("stock", "sym")):
        L.append(f"  P&L by {label}:")
        agg: dict[str, list] = {}
        for t in graded:
            agg.setdefault(t[key], []).append(t["pnl"])
        for k, v in sorted(agg.items(), key=lambda kv: sum(kv[1])):
            L.append(f"    {k:<16} {len(v):>3} trades  {money(sum(v))}")
    days = sorted({et_day(t["exit_t"]) for t in graded})
    q, miss = qqq_days(days, bars)
    L.append(f"  QQQ over the same {len(days)} trading days (open to close each day, summed): {q:+.2f}%"
             + (f" ({len(miss)} day(s) without QQQ bars)." if miss else ".") + " SPY not archived.")
    if folder:                                    # Addendum A4 control arm, reported only (never changes the gates)
        L += [""] + control_arm_lines(folder, end_day=days[-1] if days else None, reading=True)
    calls: dict[str, int] = {}
    for d in decisions:
        if d.get("trigger") in NOT_CLAUDE_CALLS or d.get("action") == "-":
            continue                      # engine rows (stop fills, close-out) and skipped asks are not Claude calls
        if "t" in d and et_day(d["t"]) in days:
            calls[et_day(d["t"])] = calls.get(et_day(d["t"]), 0) + 1
    if calls:
        L.append(f"  Claude calls per day (real calls only: self-tests, stop fills, close-outs and skipped asks excluded): average "
                 f"{statistics.fmean([calls.get(d, 0) for d in days]):.1f} over {len(days)} days.")
    return L


def build_report(folder: str, seed: int = SEED, n_copies: int = N_COPIES) -> tuple[str, dict]:
    trades = load_trades(folder)
    decisions = load_decisions(folder)
    bars = Bars(folder)
    stops_known = attach_stops(trades, decisions)
    n = len(trades)
    with_bars = sum(1 for t in trades if bars.sym(et_day(t["entry_t"]), t["sym"]))
    ddays = desk_days(trades, decisions)
    per_day = n / len(ddays) if ddays else 0.0
    reached = [(k, name) for k, name in READS if n >= k]
    nxt = next(((k, name) for k, name in READS if n < k), None)
    needed, next_name = nxt if nxt else (READS[-1][0], READS[-1][1] + " reached")
    est = math.ceil((needed - n) / per_day) if (nxt and per_day > 0) else (0 if not nxt else None)
    r = research(folder)
    stamp = datetime.now(ET).strftime("%a %d %b %Y %I:%M %p ET")
    L = [f"DAY DESK GRADE - {stamp}", ""]
    if not reached:
        L += ["PROGRESS ONLY - nothing is graded until 60 closed trades (pre-registered rule).", "",
              f"Closed trades: {n} of 60.",
              f"Trades per trading day: {per_day:.1f} (over {len(ddays)} day(s) the desk logged activity).",
              "Estimated trading days to Read 1: " + (f"about {est}." if est is not None else "unknown yet."),
              f"Plumbing health: {with_bars} of {n} trades have saved 1-minute bars; "
              f"{stops_known} of {n} have a known stop (matched to their buy record).",
              "  (Saved bars are needed to replay each trade at random times; the stop is needed for the "
              "stop-applied comparison.)"]
        L += [""] + control_arm_lines(folder)
        L += research_lines(r)
    else:
        L += ["Terms: P&L = profit and loss in dollars after costs. A 'copy' replays every real trade on the same "
              "stock, day, share count and holding time but at a random entry minute between 9:45 and the latest "
              "minute that still closes by 3:45 (min of 3:30 and 3:45 minus the hold - see ruling in the script). "
              "A percentile is the share of copies the real result beat.", ""]
        L.append(f"Closed trades: {n}. Stops known: {stops_known} of {n}; unknown: {n - stops_known}. "
                 f"Trades with saved bars: {with_bars} of {n}.")
        for k, name in reached:
            L.append("")
            L += read_section(name, k, trades[:k], n - k, bars, decisions, seed, n_copies, folder)
        if nxt:
            L += ["", f"Next: {next_name} at {needed} trades ({n} so far, about "
                      f"{est if est is not None else '?'} trading days away)."]
        L += [""] + research_lines(r)
    prog = {"closed": n, "needed": needed, "read": next_name, "trades_per_day": round(per_day, 2),
            "est_days_left": est, "bars_coverage": with_bars, "stops_known": stops_known,
            "research_graded": r["graded_high"], "research_needed": RESEARCH_NEEDED, "updated_t": time.time()}
    return "\n".join(L) + "\n", prog


def write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    for i in range(20):                      # Windows refuses the swap while a reader has the file open: retry 2 s
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.1)


def main(folder: str = HERE) -> str:
    text, prog = build_report(folder)
    os.makedirs(os.path.join(folder, "data"), exist_ok=True)
    write_atomic(os.path.join(folder, "data", "grade_report.txt"), text)
    write_atomic(os.path.join(folder, "data", "grade_progress.json"), json.dumps(prog, indent=2))
    print(text, end="")
    return text


# ---------------------------------------------------------------- selftest (synthetic data only)
def _synth(folder: str, n_trades: int, days: list[str], seed: int, mode: str, drop_days: tuple = ()) -> None:
    """Fake bars (random walk) and fake trades. mode 'best' = each trade buys the best minute and sells the best
    later minute for its hold; mode 'random' = entry drawn exactly like a copy (with a different seed)."""
    rng = random.Random(seed)
    os.makedirs(os.path.join(folder, "data", "bars"), exist_ok=True)
    syms = ["AAA", "BBB", "QQQ"]
    allbars = {}
    for d in days:
        base = datetime.fromisoformat(d + "T09:30:00").replace(tzinfo=ET).timestamp()
        day_bars = {}
        for s in syms:
            px, bs = 100.0, []
            for i in range(390):
                o = px
                px = max(1.0, px * (1 + rng.gauss(0, 0.0015)))
                bs.append({"t": base + 60 * i, "o": o, "h": max(o, px) * 1.0003, "l": min(o, px) * 0.9997,
                           "c": px, "v": 1000})
            day_bars[s] = bs
        allbars[d] = day_bars
        if d not in drop_days:
            with open(os.path.join(folder, "data", "bars", d + ".json"), "w", encoding="utf-8") as f:
                json.dump({"day": d, "bars": day_bars}, f)
    rows, decs = [], []
    for i in range(n_trades):
        d, s = days[i % len(days)], syms[i % 2]
        bm = {et_minute(b["t"]): b for b in allbars[d][s]}
        hold = rng.choice([5, 15, 30, 60])
        lo, hi = entry_window(hold)
        if mode == "best":
            m = max(range(lo, hi + 1), key=lambda k: bm[k + hold]["c"] - bm[k]["c"])
        else:
            m = rng.randint(lo, hi)
        raw_in, raw_out = bm[m]["c"], bm[m + hold]["c"]
        e, x = raw_in * (1 + COST_PER_SIDE), raw_out * (1 - COST_PER_SIDE)
        et = bm[m]["t"] + 60
        rows.append({"sym": s, "shares": 10, "entry": e, "exit": x, "entry_t": et, "exit_t": et + hold * 60,
                     "pnl": round(10 * (x - e), 2), "pnl_pct": 0, "why_in": "", "why_out": "", "setup": "synthetic"})
        decs.append({"t": et, "sym": s, "action": "buy", "price": raw_in, "stop": raw_in * 0.99,
                     "target": raw_in * 1.02, "shares": 10, "result": "bought"})
    with open(os.path.join(folder, "data", "trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["sym"])
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(folder, "data", "decisions.jsonl"), "w", encoding="utf-8") as f:
        for dct in decs:
            f.write(json.dumps(dct) + "\n")


SYN_DAYS = ["2030-03-04", "2030-03-05", "2030-03-06", "2030-03-07", "2030-03-08",
            "2030-03-11", "2030-03-12", "2030-03-13", "2030-03-14", "2030-03-15"]


def selftest() -> str:
    checks = []

    def ok(cond: bool, what: str) -> None:
        if not cond:
            raise AssertionError("SELFTEST FAILED: " + what)
        checks.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        # 1. perfect timing beats ~all copies
        f1 = os.path.join(tmp, "best")
        _synth(f1, 60, SYN_DAYS, 1, "best")
        tr = load_trades(f1)
        attach_stops(tr, load_decisions(f1))
        g = grade(tr, Bars(f1), n=500)
        ok(g["pct"] >= 0.99 and g["g2"] is True, f"planted best-timing record beats {100 * g['pct']:.1f}% of copies")
        ok(abs(g["floor"] - (p95(g["plain"]) - statistics.median(g["plain"]))) < 1e-9, "floor = p95 - median")
        g_again = grade(tr, Bars(f1), n=500)
        ok(g_again["plain"] == g["plain"] and g_again["stopped"] == g["stopped"], "same seed gives identical copies")
        # 2. random timing lands mid-distribution
        f2 = os.path.join(tmp, "rand")
        _synth(f2, 60, SYN_DAYS, 2, "random")
        tr2 = load_trades(f2)
        g2 = grade(tr2, Bars(f2), n=500)
        ok(0.05 < g2["pct"] < 0.95, f"copy-like record lands mid-distribution ({100 * g2['pct']:.1f}%)")
        # 3. missing days: 1 of 10 days (10%) still graded; 2 of 10 (20%) -> UNKNOWN
        f3 = os.path.join(tmp, "drop1")
        _synth(f3, 60, SYN_DAYS, 3, "random", drop_days=(SYN_DAYS[0],))
        g3 = grade(load_trades(f3), Bars(f3), n=200)
        ok(g3["n_left_out"] == 6 and g3["g2"] is not None, "6 of 60 trades without bars (10%) left out, G2 still graded")
        f4 = os.path.join(tmp, "drop2")
        _synth(f4, 60, SYN_DAYS, 4, "random", drop_days=tuple(SYN_DAYS[:2]))
        g4 = grade(load_trades(f4), Bars(f4), n=200)
        ok(g4["n_left_out"] == 12 and g4["g2"] is None and g4["verdict"] == "UNKNOWN",
           "12 of 60 trades without bars (20%) -> G2 UNKNOWN, not PASS")
        # 4. window ruling
        ok(entry_window(5) == (585, 930) and entry_window(60) == (585, 885) and entry_window(400) == (585, 585),
           "entry window = 9:45 .. min(15:30, 15:45 - hold), 9:45 only if empty")
        # 5. stop fill rule (gap through fills at the open)
        bm = {600: {"o": 100, "h": 100, "l": 100, "c": 100}, 601: {"o": 99.5, "h": 99.6, "l": 99.4, "c": 99.5},
              602: {"o": 97.0, "h": 97.2, "l": 96.5, "c": 97.0}, 603: {"o": 97, "h": 97, "l": 97, "c": 97}}
        p = copy_pnl(bm, 600, 3, 1, stop_dist=2.0)
        ok(abs(p - (97.0 * (1 - COST_PER_SIDE) - 100 * (1 + COST_PER_SIDE))) < 1e-9, "stop gapped through fills at open")
        # 6. guard: below 60 trades no verdict words
        f5 = os.path.join(tmp, "few")
        _synth(f5, 59, SYN_DAYS, 5, "best")
        text, prog = build_report(f5)
        ok("PROGRESS ONLY" in text and not any(w in text.upper() for w in ("PASS", "FAIL", "PERCENTILE", "VERDICT")),
           "59 trades -> progress only, no verdict words")
        ok(prog["closed"] == 59 and prog["needed"] == 60, "progress JSON counts")
    msg = f"SELFTEST OK - {len(checks)} checks passed:\n" + "\n".join("  - " + c for c in checks)
    return msg


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        print(selftest())
    else:
        main()
