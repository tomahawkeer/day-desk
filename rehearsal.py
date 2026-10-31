"""REHEARSAL.bat — a whole trading day replayed through the REAL desk code, with a scripted stand-in for Claude.

Why (10-31): the desk's code changes a lot between trading days, and the tests check pieces one at a time. This runs
the pieces TOGETHER the way Monday will: research at 8:45, the scanner waking "Claude", buys and refusals, resting
stops and targets, re-entry locks, protect-gain questions, a program restart at lunch with positions open, the 3:45
close-out, bar archive, trade lab, decision score, trigger lab, the end-of-day review, the 3:50 recap, phone alerts and
the heartbeat. Prices come from a saved day in data/bars/<day>.json, served minute by minute on a simulated clock.

ZERO Claude calls and nothing real touched: it runs in a temporary folder with a copy of config.json and the current
playbook. The stand-in "Claude" buys, sells, adjusts and passes at random (seeded), sometimes answers garbage or times
out, so the error paths run too. Its trades mean NOTHING about strategy; the point is whether anything breaks or any
rule is broken. Ends with a PASS/FAIL list and writes data/rehearsal_report.txt.

  REHEARSAL.bat                 the most recent saved day
  REHEARSAL.bat --day 2026-10-29
  REHEARSAL.bat --all           every saved day (a few minutes)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import shutil
import sys
import tempfile
import time as _real_time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import day_desk   # noqa: E402
import health     # noqa: E402
from brain import parse_decision, parse_research, parse_review   # noqa: E402
from engine import ET, minutes_et, hm   # noqa: E402


class Clock:
    def __init__(self, t: float):
        self.t = t


class SimTime:
    """Stands in for the `time` module inside day_desk/health so every 'now' is the simulated clock."""
    def __init__(self, clock: Clock):
        self._c = clock

    def time(self) -> float:
        return self._c.t

    def sleep(self, s: float) -> None:
        self._c.t += s

    def __getattr__(self, name):
        return getattr(_real_time, name)


class ReplayFeed:
    """Serves a saved day's 1-minute bars up to the simulated time (the minute in progress included, as Yahoo
    does). Optionally fails for a few minutes so the red-pill / retry path runs."""
    def __init__(self, bars: dict, clock: Clock, fail_from: float | None = None, fail_to: float | None = None):
        self.bars, self.c, self.fail = bars, clock, (fail_from, fail_to)
        self.calls = 0

    def bars_1m(self, syms):
        self.calls += 1
        if self.fail[0] and self.fail[0] <= self.c.t < self.fail[1]:
            raise RuntimeError("simulated Yahoo outage")
        return {s: [b for b in self.bars.get(s, []) if b["t"] <= self.c.t] for s in syms}

    def news(self, sym):
        return [f"{datetime.fromtimestamp(self.c.t, ET):%m-%d %H:%M} ET | (rehearsal) no real headlines"]


class ScriptedBrain:
    """A stand-in for Claude: random but rule-shaped answers, a 5-40 s 'thinking' delay on the simulated clock,
    the odd garbage reply or timeout. Never calls anything."""
    SETUPS = ["vwap-reclaim", "breakout-hold", "flush-reclaim", "momentum-push"]

    def __init__(self, clock: Clock, seed: int, picks: list[str]):
        self.c, self.rng, self.picks = clock, random.Random(seed), picks
        self.desk = None
        self.sym = None
        self.counts = {"decide": 0, "review": 0, "research": 0, "garbage": 0, "timeout": 0}
        self._usage = None

    def last_usage(self):
        return self._usage

    def _think(self, lo=5, hi=40):
        self.c.t += self.rng.uniform(lo, hi)

    def decide(self, prompt: str):
        self.counts["decide"] += 1
        self._think()
        self._usage = {"tok_in": 4200, "tok_new": 4200, "tok_cached": 0, "tok_out": 180, "cost_usd": 0.07,
                       "secs": 12.0, "turns": 1, "searches": 0}
        r = self.rng.random()
        if r < 0.02:
            self.counts["timeout"] += 1
            raise RuntimeError("simulated: Claude Code timed out")
        if r < 0.04:
            self.counts["garbage"] += 1
            return parse_decision("I think you should probably buy, maybe."), "garbage"     # raises: no JSON
        d = self.desk
        px = d.prices.get(self.sym) or 100.0
        held = self.sym in d.book.s["positions"]
        x = self.rng.random()
        if held:
            pos = d.book.s["positions"][self.sym]
            if x < 0.2:
                ans = {"action": "sell", "why": "rehearsal: taking it off"}
            elif x < 0.45:
                ans = {"action": "adjust", "stop": round(max(pos["stop"], px * 0.996), 2), "why": "rehearsal: trail"}
            elif x < 0.5:
                ans = {"action": "adjust", "stop": round(pos["stop"] * 0.98, 2), "why": "rehearsal: try to LOWER (must be refused)"}
            else:
                ans = {"action": "hold", "why": "rehearsal: holding"}
        else:
            self.fresh_asks = getattr(self, "fresh_asks", 0) + 1
            if self.fresh_asks == 1:                                  # always: one order the engine must refuse
                txt = json.dumps({"action": "buy", "setup": "too-wide", "stop": round(px * 0.95, 2),
                                  "target": round(px * 1.1, 2), "why": "rehearsal: stop 5% away (must be refused)"})
                return parse_decision(txt), txt
            if self.fresh_asks == 2:
                x = 0.0                                               # always: one good buy, so the buy path runs
            if x < 0.35:
                dist = self.rng.uniform(0.003, 0.012)
                if self.rng.random() < 0.05:
                    dist = 0.05                                       # too wide: the engine must refuse it
                ans = {"action": "buy", "setup": self.rng.choice(self.SETUPS), "stop": round(px * (1 - dist), 2),
                       "target": round(px * (1 + 2 * dist), 2), "rules": ["R1"], "p_win": self.rng.randint(35, 65),
                       "why": "rehearsal: scripted buy"}
                if self.rng.random() < 0.1:
                    ans["shares"] = 10_000                            # oversized: the engine must cap it
            else:
                ans = {"action": "pass", "p_win": self.rng.randint(20, 50), "why": "rehearsal: pass"}
        txt = json.dumps(ans)
        return parse_decision(txt), txt

    def review(self, prompt: str) -> dict:
        self.counts["review"] += 1
        self.review_prompt = prompt
        self._think(30, 90)
        self._usage = {"tok_in": 30000, "tok_out": 1500, "cost_usd": 0.6, "turns": 1, "searches": 0}
        return parse_review(json.dumps({
            "summary": "Rehearsal review: scripted trades, nothing to learn.",
            "lessons": [{"id": "R1", "rule": "Rehearsal rule one.", "evidence": "rehearsal", "status": "solid"},
                        {"rule": "Rehearsal rule two (new).", "evidence": "rehearsal", "status": "tentative"}],
            "strategies": [{"setup": s, "verdict": "needs more data", "note": "rehearsal"} for s in self.SETUPS]}))

    def research(self, prompt: str, model=None) -> dict:
        self.counts["research"] += 1
        self._think(60, 180)
        self._usage = {"tok_in": 90000, "tok_out": 3000, "cost_usd": 1.2, "turns": 12, "searches": 9}
        picks = [{"ticker": t, "catalyst": "rehearsal", "why_watch": "rehearsal", "setup": "x", "avoid_if": "y",
                  "levels": "", "source": "rehearsal", "conviction": "high" if i == 0 else "medium"}
                 for i, t in enumerate(self.picks)]
        return parse_research(json.dumps({"backdrop": "Rehearsal backdrop.", "picks": picks}))

    def ping(self) -> str:
        return '{"ok": true, "says": "DAY DESK OK"}'


class RecNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, event, title, body):
        self.sent.append((event, title, body))
        return True

    def status(self):
        return {"enabled": True, "topic": "rehearsal", "sent": len(self.sent)}

    def __getattr__(self, name):                 # anything else the desk asks of a notifier: harmless no-op
        return lambda *a, **k: None


def bar_days(folder: str) -> list[str]:
    return sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(folder, "data", "bars", "*.json")))


def rehearse(day: str, folder: str = HERE, seed: int = 7, restart_at: str = "12:30", verbose: bool = True) -> dict:
    """One full day. Returns {"day", "checks": [(ok, text)], "stats": {...}}."""
    with open(os.path.join(folder, "data", "bars", day + ".json"), encoding="utf-8") as f:
        saved = json.load(f)["bars"]
    base = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ET).timestamp()
    clock = Clock(base + 8 * 3600 + 40 * 60)
    tmp = tempfile.mkdtemp(prefix="day-desk-rehearsal-")
    os.makedirs(os.path.join(tmp, "data", "bars"), exist_ok=True)
    shutil.copy(os.path.join(folder, "config.json"), tmp)
    for name in ("lessons.json",):                  # today's real playbook, so prompts are realistic size
        p = os.path.join(folder, "data", name)
        if os.path.exists(p):
            shutil.copy(p, os.path.join(tmp, "data", name))
    for p in glob.glob(os.path.join(folder, "data", "bars", "*.json")):   # earlier days, for the labs
        if os.path.basename(p)[:-5] < day:
            shutil.copy(p, os.path.join(tmp, "data", "bars"))

    real_time_dd, real_time_h = day_desk.time, health.time
    day_desk.time = SimTime(clock)
    health.time = SimTime(clock)
    checks: list[tuple[bool, str]] = []
    errors: list[str] = []
    snapshots = 0
    try:
        cfg = day_desk.load_config(tmp)
        watch = ["NVDA", "TSLA", "AMD", "META", "AAPL"]
        others = [s for s in sorted(saved) if s not in watch and s != "QQQ"]
        picks = others[:3]
        feed = ReplayFeed(saved, clock, fail_from=base + 11 * 3600, fail_to=base + 11 * 3600 + 150)
        brain = ScriptedBrain(clock, seed * 1000 + int(day.replace("-", "")) % 997, picks)
        notes = RecNotifier()

        def make():
            d = day_desk.Desk(tmp, cfg, feed, brain, notes)
            d._sync_research = True
            brain.desk = d
            orig = d.ask

            def ask(sym, *a, **k):
                brain.sym = sym
                return orig(sym, *a, **k)
            d.ask = ask
            return d

        desk = make()
        desk.set_watchlist(watch)
        rh, rm = (int(x) for x in restart_at.split(":"))
        restart_t = base + rh * 3600 + rm * 60
        restarted = False
        feed_failed_seen = False
        end = base + 16 * 3600 + 20 * 60
        while clock.t < end:
            if not restarted and clock.t >= restart_t:
                desk = make()                           # the program restarts mid-day: state must survive
                restarted = True
            try:
                desk.step(clock.t)
                feed_failed_seen = feed_failed_seen or desk.status.get("feed_ok") is False
            except Exception:
                errors.append(f"{hm(clock.t)} step crashed: " + traceback.format_exc(limit=3).strip().splitlines()[-1])
            try:
                desk.health.loop_done(clock.t)
                desk.health.watch_problem(clock.t)
                if int(clock.t) % 600 < 30:
                    json.dumps(desk.snapshot(), default=str)    # the page's data must always build
                    snapshots += 1
            except Exception:
                errors.append(f"{hm(clock.t)} heartbeat/page crashed: " +
                              traceback.format_exc(limit=3).strip().splitlines()[-1])
            clock.t += cfg["poll_seconds"]

        # ---------------- checks ----------------
        D = os.path.join(tmp, "data")
        def ok(cond, text):
            checks.append((bool(cond), text))

        ok(not errors, f"no crashes ({len(errors)} found)" + ("".join("\n      " + e for e in errors[:8])))
        trades = desk.book.trades()
        rows = [json.loads(l) for l in open(os.path.join(D, "decisions.jsonl"), encoding="utf-8")] \
            if os.path.exists(os.path.join(D, "decisions.jsonl")) else []
        buys = [r for r in rows if str(r.get("result", "")).startswith("bought")]
        refused = [r for r in rows if str(r.get("result", "")).startswith("refused")]
        ok(restarted and not desk.book.s["positions"], "flat at the end of the day, after a lunchtime restart")
        late = [r for r in buys if minutes_et(r["t"]) >= cfg["last_buy_minute"]]
        ok(not late, f"no buys at or after {cfg['last_buy_minute'] // 60}:{cfg['last_buy_minute'] % 60:02d} ({len(late)} found)")
        too_big = [t for t in trades if t["shares"] * t["entry"] > cfg["max_position_usd"] * 1.0005]
        ok(not too_big, f"no position over ${cfg['max_position_usd']} ({len(too_big)} found)")
        wide = [r for r in buys if r.get("stop") and r.get("fill_price")
                and (1 - r["stop"] / r["fill_price"]) * 100 > cfg["max_stop_pct"] + 1e-6]
        ok(not wide, f"no buy with a stop over {cfg['max_stop_pct']}% away ({len(wide)} found)")
        late_exit = [t for t in trades if minutes_et(t["exit_t"]) > cfg["flatten_minute"] + 1]
        ok(not late_exit, f"every trade closed by the close-out ({len(late_exit)} late)")
        ok(desk.book.books_problem() is None, "cash reconciles with the trade history")
        ok(desk.book.s["calls_today"] <= cfg["max_calls_per_day"], f"Claude calls within the cap ({desk.book.s['calls_today']})")
        # re-entry lock: no buy in a stock within the lock window after its stop
        rebuys = []
        for t in trades:
            if str(t["why_out"]).startswith("stop hit"):
                for r in buys:
                    if r["sym"] == t["sym"] and t["exit_t"] < r["t"] < t["exit_t"] + cfg["reentry_lock_after_stop_min"] * 60:
                        rebuys.append(f"{r['sym']} {hm(r['t'])}")
        ok(not rebuys, f"no re-buy inside the re-entry lock ({', '.join(rebuys) or 'none'})")
        ok(any(r.get("setup") == "too-wide" and str(r.get("result", "")).startswith("refused") for r in rows),
           "the engine refused the deliberately bad order (stop 5% away)")
        ok(buys, f"the buy path ran ({len(buys)} buys)")
        ok(brain.counts["research"] == 1, f"morning research ran once ({brain.counts['research']})")
        ok(os.path.exists(os.path.join(D, "pick_results.jsonl")), "research picks graded at the close-out")
        ok(os.path.exists(os.path.join(D, "bars", day + ".json")), "today's bars archived")
        for name in ("lab_report.json", "decision_score.json", "trigger_lab.json"):
            ok(os.path.exists(os.path.join(D, name)), f"close-out rebuilt {name}")
        ok(brain.counts["review"] == 1, f"end-of-day review ran once ({brain.counts['review']})")
        rp = getattr(brain, "review_prompt", "")
        ok("MEASURED FACTS" in rp and "RULE CREDIT" in rp, "the review saw MEASURED FACTS and RULE CREDIT")
        asks = [r for r in rows if r.get("action") in ("buy", "pass", "sell", "hold", "adjust") and "v" in r]
        ok(asks and all("rules" in r for r in asks), f"every answered call carries version stamp + rules ({len(asks)})")
        ok(all(r.get("fill_price") for r in buys), "every buy filled at the price re-checked after the answer")
        ok(all("usage" in r for r in asks), "every answered call logged its usage")
        L = json.load(open(os.path.join(D, "lessons.json"), encoding="utf-8")) if os.path.exists(os.path.join(D, "lessons.json")) else {}
        ids = [l.get("id") for l in L.get("lessons", [])]
        ok(ids and all(ids) and len(set(ids)) == len(ids), f"playbook rules all carry unique ids ({ids})")
        unearned = [l["id"] for l in L.get("lessons", []) if l.get("status") == "solid"]
        ok(not unearned or len(trades) >= 3, f"no unearned 'solid' rule ({unearned})")
        events = [e for e, _, _ in notes.sent]
        ok(events.count("recap") == 1, f"one 3:50 recap alert ({events.count('recap')})")
        ok(events.count("research") == 1, "one research alert")
        ok(len([r for r in rows if str(r.get("result", "")).startswith("bought")]) == events.count("buy"),
           "a phone alert for every buy")
        ok(feed_failed_seen and desk.status.get("feed_ok") is True and "outage" not in str(desk.status.get("error")),
           "price feed failure showed, then recovered (simulated 11:00 outage)")
        hb = desk.health.heartbeat(clock.t)["verdict"]
        ok(hb.startswith("WORKING") or "CLAUDE CAN'T" in hb, f"heartbeat at the end: {hb[:90]}")
        ok(snapshots > 10, f"the phone page's data built {snapshots} times without error")
        stats = {"trades": len(trades), "pnl": round(sum(t["pnl"] for t in trades), 2), "buys": len(buys),
                 "refused": len(refused), "calls": desk.book.s["calls_today"], "alerts": len(notes.sent),
                 "garbage": brain.counts["garbage"], "timeouts": brain.counts["timeout"],
                 "stops": sum(1 for t in trades if str(t["why_out"]).startswith("stop hit")),
                 "protect_gain_asks": sum(1 for r in rows if r.get("trigger") == "protect-gain")}
    finally:
        day_desk.time, health.time = real_time_dd, real_time_h
        shutil.rmtree(tmp, ignore_errors=True)
    return {"day": day, "checks": checks, "stats": stats}


def render(res: dict) -> str:
    s = res["stats"]
    bad = [c for c in res["checks"] if not c[0]]
    lines = [f"REHEARSAL {res['day']}: {'PASS' if not bad else 'FAIL'} ({len(res['checks']) - len(bad)} of {len(res['checks'])} checks)",
             f"  stand-in Claude: {s['calls']} calls, {s['buys']} buys, {s['refused']} refused by the engine, "
             f"{s['trades']} closed trades ({s['stops']} stops), {s['protect_gain_asks']} protect-gain asks, "
             f"{s['garbage']} garbage answers, {s['timeouts']} timeouts, {s['alerts']} phone alerts",
             f"  (its P&L {s['pnl']:+.2f} means NOTHING: the answers are random)"]
    for good, text in res["checks"]:
        lines.append(f"  [{'ok' if good else 'FAIL'}] {text}")
    return "\n".join(lines)


def main(argv=None, folder: str = HERE) -> int:
    ap = argparse.ArgumentParser(description="Replay a saved trading day through the whole desk (zero Claude calls).")
    ap.add_argument("--day")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    days = bar_days(folder)
    if not days:
        print("No saved days in data\\bars yet (the desk saves one at every 3:45 close-out).")
        return 1
    todo = days if a.all else [a.day or days[-1]]
    out, fails = [], 0
    for d in todo:
        print(f"Rehearsing {d} ...", flush=True)
        r = rehearse(d, folder, seed=a.seed)
        txt = render(r)
        print(txt + "\n")
        out.append(txt)
        fails += any(not c[0] for c in r["checks"])
    summary = f"{len(todo) - fails} of {len(todo)} day(s) passed."
    print(summary)
    with open(os.path.join(folder, "data", "rehearsal_report.txt"), "w", encoding="utf-8") as f:
        f.write(f"Written {datetime.now(ET):%Y-%m-%d %H:%M} ET\n\n" + "\n\n".join(out) + "\n\n" + summary + "\n")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
