"""Day Desk: live paper day trading, with Claude making the calls.

Run it with RUN.bat. It does three things at once:
  1. every 30 seconds, pulls 1-minute bars for your watchlist and checks stops/targets;
  2. when the scanner sees something happening, asks Claude (through Claude Code on this PC,
     on your subscription: no API key, no separate bill) what to do;
  3. serves a phone-friendly page on port 8790 (open http://<this-PC's Tailscale name>:8790).

Paper only. There is no broker connection anywhere in this program.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from datetime import datetime, timedelta

from engine import BAR_SECONDS, Book, ET, et_now, limits_problems, minutes_et, hm, scorecard
from scanner import scan, Cooldowns
from health import Health, write_json as _health_write_json
from brain import build_prompt, build_review_prompt, find_claude_cli, set_window_title, fmt_research_for

HERE = os.path.dirname(os.path.abspath(__file__))
TICKER_RE = re.compile(r"^[A-Z]{1,5}([.][A-Z])?$")
log = logging.getLogger("daydesk")


RULE_JOIN_S = 300        # a cited buy joins the trade filled within 5 min after the decision (post-answer fill)
SOLID_MIN_TRADES = 3     # a lesson may only be "solid" once 3+ closed trades that cited it exist...
SOLID_MIN_DAYS = 2       # ...on 2+ different days (the review prompt's own definition, now enforced in code)


def assign_rule_ids(lessons: list[dict], previous: list[dict], last_id: int = 0) -> int:
    """Keep R-numbers stable: an id the review kept (and that existed before) stays; anything else gets the next
    free number. Ids are never reused, so a dropped rule's history can't be inherited by a new one."""
    used = {l.get("id") for l in previous if l.get("id")}
    top = max([int(i[1:]) for i in used] + [last_id])
    seen = set()
    for l in lessons:
        rid = l.get("id")
        if rid and rid in used and rid not in seen:
            seen.add(rid)
            continue
        top += 1
        l["id"] = f"R{top}"
        seen.add(l["id"])
    return top


# decisions.jsonl rows written by the ENGINE, not by a Claude call (stops/targets filling, the 3:45 close-out,
# the overnight sweep, the research grading line). The usage summary must not count them as calls.
NOT_CLAUDE_CALLS = {"resting order", "3:45 PM close-out", "new day", "research grading", "re-entry lock"}


def write_json(path: str, obj, indent: int | None = None) -> None:
    """Write a JSON file atomically (temp file, then swap), so a reader on another thread
    can never see a half-written file."""
    _health_write_json(path, obj, indent)        # same, plus the Windows retry (health.py, 10-29)


def load_config(folder: str = HERE) -> dict:
    with open(os.path.join(folder, "config.json"), encoding="utf-8") as f:
        return json.load(f)


class Desk:
    def __init__(self, folder: str, cfg: dict, feed, brain, notifier=None):
        self.folder, self.cfg, self.feed, self.brain = folder, cfg, feed, brain
        self.notifier = notifier                     # phone alerts (notify.py); None = no alerts (tests, or off)
        self.book = Book(folder, cfg)
        self.watch_path = os.path.join(folder, "data", "watchlist.json")
        self.dec_path = os.path.join(folder, "data", "decisions.jsonl")
        self.picks_path = os.path.join(folder, "data", "picks.json")
        self.picks_hist = os.path.join(folder, "data", "picks_history.jsonl")
        self.pick_results_path = os.path.join(folder, "data", "pick_results.jsonl")
        self.research_running = False
        self.lessons_path = os.path.join(folder, "data", "lessons.json")
        self.lessons_hist = os.path.join(folder, "data", "lessons_history.jsonl")
        self.cool = Cooldowns(cfg, os.path.join(folder, "data", "cooldowns.json"))
        self.last_review: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.pick_moves: dict[str, float] = {}       # today's research picks: % since the open (watch-only included)
        self.status = {"market": "starting", "last_poll_t": None, "feed_ok": None, "error": "",
                       "brain": "live" if brain else "watch only: Claude Code not found on this PC"}
        self.recent = self._load_recent()
        self.alerts = self.startup_checks()          # red banner on the page + heartbeat if anything is wrong
        self.health = Health(self)                   # heartbeat + mailbox; main() starts its thread

    def lock_after_stop(self, sym: str, tr: dict, d: datetime) -> None:
        """No new buys in a stock for reentry_lock_after_stop_min after its stop fills; the rest of the day after
        max_stops_per_stock_per_day stops. Scanner-level change (allowed by the pre-reg, logged 10-31)."""
        day = d.date().isoformat()
        stops = sum(1 for t in self.book.trades() if t["sym"] == sym and t["why_out"].startswith("stop hit")
                    and et_now(t["exit_t"]).date().isoformat() == day)
        if stops >= self.cfg["max_stops_per_stock_per_day"]:
            until = datetime.fromisoformat(day + "T23:59:00").replace(tzinfo=ET).timestamp()
            why = f"{stops} stop-outs in {sym} today: done with it until tomorrow"
        else:
            until = tr["exit_t"] + self.cfg["reentry_lock_after_stop_min"] * 60
            why = f"stopped out at {hm(tr['exit_t'])}: no re-buy until {hm(until)}"
        self.cool.lock(sym, until, why)
        self.record({"t": tr["exit_t"], "sym": sym, "trigger": "re-entry lock", "action": "-", "result": why})

    # ---------- Claude can't be reached (10-31) ----------
    # Anthropic's docs say --bare "will become the default for -p in a future release", and --bare ignores the
    # subscription login. If that ships, every desk call fails with "Not logged in". Without this the desk would sit
    # there all day answering nothing while the page looked normal. Now: the first login-type failure, or
    # claude_fail_alert_after failures in a row of any kind, puts a red banner on the page and ATTENTION in the
    # heartbeat; Health.watch_problem then rings the phone once (after desk_problem_minutes). A good call clears it.
    LOGIN_WORDS = ("not logged in", "/login", "please run /login", "authentication", "unauthorized", "401",
                   "invalid api key", "oauth", "credit balance")

    def note_claude_result(self, ok: bool, err: str = "", now: float | None = None) -> None:
        now = now or time.time()
        tag = "CLAUDE CAN'T BE REACHED"
        if ok:
            self.claude_fails = 0
            if any(a.startswith(tag) for a in self.alerts):
                self.alerts = [a for a in self.alerts if not a.startswith(tag)]
                log.info("Claude calls are working again")
            return
        self.claude_fails = getattr(self, "claude_fails", 0) + 1
        login = any(w in err.lower() for w in self.LOGIN_WORDS)
        n = int((self.cfg.get("alerts") or {}).get("claude_fail_alert_after", 3))
        if not (login or self.claude_fails >= n) or any(a.startswith(tag) for a in self.alerts):
            return
        why = ("Claude Code says it is not logged in. Open a terminal on the PC, run claude, sign in with your "
               "subscription (NOT an API key), then restart RUN.bat." if login else
               f"{self.claude_fails} Claude calls failed in a row.")
        self.alerts = self.alerts + [f"{tag}: {why} Last error: {err[:160]}"]
        log.warning("%s: %s", tag, err[:300])     # the heartbeat thread rings the phone (one loud alert per episode)

    def startup_checks(self) -> list[str]:
        """10-31 guards (code reviews of hummingbot/nautilus): frozen money limits unchanged, cash reconciles with the
        trade history, no position left 'open' that was already sold. A changed limit PAUSES new buys."""
        out = []
        probs = limits_problems(self.cfg)
        if probs:
            with self.book.lock:
                self.book.s["paused"] = True
                self.book.save()
            out.append("LIMITS CHANGED - new buys paused: " + "; ".join(probs))
        out += [f"Books repaired: {r}" for r in self.book.repaired]
        bp = self.book.books_problem()
        if bp:
            out.append("BOOKS DON'T ADD UP: " + bp)
        for a in out:
            log.warning(a)
        return out

    # ---------- watchlist ----------
    def watchlist(self) -> list[str]:
        if os.path.exists(self.watch_path):
            with open(self.watch_path, encoding="utf-8") as f:
                return json.load(f).get("symbols", [])
        return ["NVDA", "TSLA", "AMD", "META", "AAPL"]

    def set_watchlist(self, syms: list[str]) -> str | None:
        clean = []
        for s in syms:
            s = str(s).strip().upper()
            if not TICKER_RE.match(s):
                return f"'{s}' doesn't look like a US ticker"
            if s not in clean:
                clean.append(s)
        if len(clean) > self.cfg["max_watchlist"]:
            return f"the list holds {self.cfg['max_watchlist']} tickers"
        write_json(self.watch_path, {"symbols": clean, "updated_t": time.time()})
        return None

    # ---------- morning research ----------
    def research(self) -> dict:
        if os.path.exists(self.picks_path):
            with open(self.picks_path, encoding="utf-8") as f:
                return json.load(f)
        return {"day": None, "backdrop": "", "picks": [], "status": ""}

    def todays_research(self, now: float) -> dict | None:
        r = self.research()
        return r if r.get("day") == et_now(now).date().isoformat() else None

    def research_session(self, now: float) -> tuple[str, bool]:
        """Which trading session a research run is for: (date, is_preview). Before 4 PM on a weekday the desk
        trades, it is today. Otherwise (weekend, evening, a no-trade day) it is a PREVIEW of the next weekday the
        desk trades. Added 10-31: on Saturday the research saw "market closed", ran no searches and returned nothing,
        so a weekend run could never prove the web search works. Full market holidays still answer "closed"."""
        d = et_now(now).date()
        skip = self.cfg.get("no_trade_days") or {}
        if d.weekday() < 5 and minutes_et(now) < 16 * 60 and d.isoformat() not in skip:
            return d.isoformat(), False
        for _ in range(14):
            d += timedelta(days=1)
            if d.weekday() < 5 and d.isoformat() not in skip:
                break
        return d.isoformat(), True

    def run_research(self, now: float, sync: bool = False) -> str:
        """Pre-market news research through Claude Code (web search only). Runs in the background
        so prices, stops and the page keep working while it reads. On a weekend or evening it is a PREVIEW of the
        next session: same call, saved under that session's date, never auto-adds (the 8:45 run that morning
        replaces it and does the auto-add)."""
        if self.brain is None:
            return "Claude Code not found"
        if self.research_running:
            return "already running"
        self.research_running = True
        day, preview = self.research_session(now)

        def work():
            try:
                when = f"It is {et_now(now).strftime('%A %B %d, %Y')}, {hm(now)} ET. "
                if preview:
                    nice = datetime.fromisoformat(day).strftime("%A %B %d, %Y")
                    prompt = (when + f"The US market is closed right now. This is a PREVIEW for the next session, "
                              f"{nice}: search for news since the last close and for that session's scheduled events, "
                              f"and find the best stocks to watch that day for a long day trade. "
                              f"Current watchlist: {', '.join(self.watchlist())}.")
                else:
                    prompt = (when + f"Current watchlist: {', '.join(self.watchlist())}. "
                              f"Find today's best stocks to watch for a long day trade.")
                rv = self.brain.research(prompt, self.cfg.get("research_model"))
                self.note_claude_result(True)
                out = {"day": day, "t": time.time(), "status": "done", "preview": preview, **rv}
                rec = {"t": time.time(), "sym": "-", "trigger": "morning research", "action": "research",
                       "why": rv["backdrop"], "result": ("PREVIEW for " + day + " · " if preview else "") +
                       "picks: " + (", ".join(p["ticker"] for p in rv["picks"]) or "none"),
                       "usage": self.call_usage(), "preview": preview}
                u = rec["usage"] or {}
                if not u.get("searches") and (u.get("turns") or 0) <= 1:
                    rec["result"] += " · WARNING: no web search was made"
                added = [] if preview else self.apply_auto_picks(day, rv["picks"])
                if added:
                    rec["result"] += " · auto-added (high conviction): " + ", ".join(added)
                self.alert_research(rv["picks"], added, preview)
            except Exception as e:
                self.note_claude_result(False, str(e))
                out = {"day": day, "t": time.time(), "status": f"failed: {e}"[:300], "backdrop": "", "picks": []}
                rec = {"t": time.time(), "sym": "-", "trigger": "morning research", "action": "error",
                       "result": out["status"]}
                self.alert("research", "Research failed", out["status"][:200])
            write_json(self.picks_path, out, indent=1)
            with open(self.picks_hist, "a", encoding="utf-8") as f:
                f.write(json.dumps(out) + "\n")
            self.record(rec)
            self.research_running = False

        if sync:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
        return "started"

    def apply_auto_picks(self, day: str, picks: list[dict]) -> list[str]:
        """tomahawkeer 10-28: 'You have permission if any pick ever looks to be prime to be profitable for that day.'
        High-conviction picks join the watchlist for the day; yesterday's auto-adds come off again unless still held
        or picked high again. Tickers tomahawkeer added himself are never removed."""
        mode = self.cfg.get("auto_add_picks", "high")
        if mode in (False, "off", None):
            return []
        path = os.path.join(self.folder, "data", "auto_added.json")
        auto = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        wanted = [p["ticker"] for p in picks if mode is True or mode == "all" or p.get("conviction") == "high"]
        wl = self.watchlist()
        held = set(self.book.s["positions"])
        for t, d0 in list(auto.items()):                 # yesterday's auto-adds go, unless held or re-picked
            if d0 != day and t not in held and t not in wanted:
                if t in wl:
                    wl.remove(t)
                auto.pop(t)
        added = []
        for t in wanted:
            if t not in wl and len(wl) < self.cfg["max_watchlist"]:
                wl.append(t); added.append(t); auto[t] = day
            elif t in auto:
                auto[t] = day
        self.set_watchlist(wl)
        write_json(path, auto)
        return added

    # ---------- grading the research (10-30) ----------
    # tomahawkeer 10-30 approved: "record prices for every pick, watch-only". On 10-29 only the auto-added pick had prices,
    # so four of five picks could not be graded. Now every pick of the day is priced with the watchlist. Watch-only
    # picks are NEVER scanned, never asked about and never bought: engine.buy still refuses anything off the watchlist.
    def watch_only_picks(self, now: float, watch: list[str]) -> list[str]:
        if not self.cfg.get("price_all_picks", True):
            return []
        r = self.todays_research(now)
        if not r:
            return []
        out = [p["ticker"] for p in r.get("picks", []) if p.get("ticker") and p["ticker"] not in watch]
        return out[: int(self.cfg.get("max_watch_only_picks", 8))]

    def update_pick_moves(self, now: float, bars: dict) -> None:
        """Live % move of every pick since the open (for the page)."""
        r = self.todays_research(now)
        moves = {}
        for p in (r or {}).get("picks", []):
            b = bars.get(p.get("ticker")) or []
            if b and b[0]["o"]:
                moves[p["ticker"]] = round((b[-1]["c"] / b[0]["o"] - 1) * 100, 2)
        self.pick_moves = moves

    @staticmethod
    def _price_at(b: list[dict], t: float) -> float | None:
        """Close of the last bar that started at or before t (None if there is none)."""
        px = None
        for x in b:
            if x["t"] <= t:
                px = x["c"]
            else:
                break
        return px

    def score_picks(self, day: str, bars: dict, now: float) -> None:
        """Once a day at the close-out: one line per pick in data/pick_results.jsonl.
        Graded window = 9:45 (scanner start, the first minute the desk could act) to 3:45 (close-out),
        measured against QQQ over the same window. Open-to-close is reported too."""
        if self.book.s.get("picks_scored_day") == day:
            return
        r = self.todays_research(now)
        with self.book.lock:
            self.book.s["picks_scored_day"] = day          # once a day, even if there is nothing to score
            self.book.save()
        if not r or not r.get("picks"):
            return
        base = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ET).timestamp()
        t_in = base + self.cfg["scanner"]["start_minute"] * 60
        t_out = base + self.cfg["flatten_minute"] * 60

        def window(b):
            a, z = self._price_at(b, t_in), self._price_at(b, t_out)
            return round((z / a - 1) * 100, 3) if a and z else None

        qqq = window(bars.get("QQQ") or [])
        wl, auto = self.watchlist(), self.auto_added()
        path = os.path.join(self.folder, "data", "pick_results.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for p in r["picks"]:
                t = p.get("ticker")
                b = bars.get(t) or []
                mv = window(b)
                row = {"day": day, "ticker": t, "conviction": p.get("conviction", "medium"),
                       "preview": bool(r.get("preview")),
                       "tradable": t in wl, "auto_added": auto.get(t) == day,
                       "bars": len(b), "open": b[0]["o"] if b else None,
                       "px_945": self._price_at(b, t_in), "px_345": self._price_at(b, t_out),
                       "high": max(x["h"] for x in b) if b else None, "low": min(x["l"] for x in b) if b else None,
                       "move_945_345_pct": mv, "qqq_945_345_pct": qqq,
                       "vs_qqq_pct": round(mv - qqq, 3) if mv is not None and qqq is not None else None,
                       "open_to_345_pct": round((b[-1]["c"] / b[0]["o"] - 1) * 100, 3) if b and b[0]["o"] else None}
                f.write(json.dumps(row) + "\n")
        self.record({"t": now, "sym": "-", "trigger": "research grading", "action": "-",
                     "result": "picks priced: " + ", ".join(
                         f"{p['ticker']} {self.pick_moves.get(p['ticker'], 0):+.1f}%" if p["ticker"] in self.pick_moves
                         else f"{p['ticker']} no data" for p in r["picks"])})

    def archive_bars(self, day: str, bars: dict) -> None:
        """Once a day at the close-out: save every 1-minute bar the desk saw today to data/bars/<day>.json.
        The pre-registered grade replays each real trade at random times on the same stock and session, and Yahoo
        only serves 1-minute bars for about 30 days back, so without this file the grade could not be computed."""
        if self.book.s.get("bars_saved_day") == day:
            return
        with self.book.lock:
            self.book.s["bars_saved_day"] = day
            self.book.save()
        folder = os.path.join(self.folder, "data", "bars")
        os.makedirs(folder, exist_ok=True)
        write_json(os.path.join(folder, day + ".json"), {"day": day, "bars": {s: b for s, b in bars.items() if b}})

    def auto_added(self) -> dict:
        path = os.path.join(self.folder, "data", "auto_added.json")
        return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}

    def maybe_morning_research(self, now: float) -> None:
        d = et_now(now)
        if d.weekday() >= 5 or minutes_et(now) < self.cfg.get("research_minute", 525) or minutes_et(now) >= 16 * 60:
            return
        day = d.date().isoformat()
        if self.book.s.get("research_day") == day or day in (self.cfg.get("no_trade_days") or {}):
            return
        with self.book.lock:
            self.book.s["research_day"] = day          # once per trading morning, even if it fails
            self.book.save()
        self.run_research(now, sync=getattr(self, "_sync_research", False))

    # ---------- what Claude has learned ----------
    def lessons(self) -> dict:
        if os.path.exists(self.lessons_path):
            with open(self.lessons_path, encoding="utf-8") as f:
                L = json.load(f)
            if any(not l.get("id") for l in L.get("lessons") or []):   # playbooks written before 10-31: give ids once
                L["last_rule_id"] = assign_rule_ids(L["lessons"], [], L.get("last_rule_id", 0))
                write_json(self.lessons_path, L, indent=1)
            return L
        return {"day": None, "summary": "", "lessons": []}

    def rule_credit(self) -> dict:
        """Per playbook rule: how often decisions cited it and what the cited BUYS did once closed (10-31; idea from
        FinMem's memory crediting, without its arbitrary scoring). Buys are joined to trades on the same stock filled
        within RULE_JOIN_S of the decision (the post-answer fill can land up to a call's length later)."""
        trades = self.book.trades()
        out: dict[str, dict] = {}
        for r in self._all_decisions():
            for rid in r.get("rules") or []:
                c = out.setdefault(rid, {"cited": 0, "passes": 0, "trades": 0, "wins": 0, "pnl": 0.0, "days": set()})
                c["cited"] += 1
                if r.get("action") == "pass":
                    c["passes"] += 1
                if r.get("action") == "buy" and str(r.get("result", "")).startswith("bought"):
                    tr = next((t for t in trades if t["sym"] == r.get("sym")
                               and 0 <= t["entry_t"] - r["t"] <= RULE_JOIN_S), None)
                    if tr:
                        c["trades"] += 1
                        c["wins"] += tr["pnl"] > 0
                        c["pnl"] = round(c["pnl"] + tr["pnl"], 2)
                        c["days"].add(et_now(tr["exit_t"]).date().isoformat())
        for c in out.values():
            c["days"] = len(c["days"])
        return out

    def _all_decisions(self) -> list[dict]:
        out = []
        if os.path.exists(self.dec_path):
            with open(self.dec_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out

    def end_of_day_review(self, day: str, bars: dict, now: float) -> None:
        """Once a day after the close-out: Claude reviews its own trades and passes
        (with what each stock did next) and rewrites its playbook."""
        if self.brain is None or self.book.s.get("reviewed_day") == day:
            return
        with self.book.lock:
            self.book.s["reviewed_day"] = day          # mark first: never retry-loop on a failure
            self.book.save()
        start = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ET).timestamp()
        todays_dec = [r for r in self.recent if r.get("t", 0) >= start and r.get("trigger") != "self-test"]
        todays_tr = [t for t in self.book.trades() if t["exit_t"] >= start]
        if not todays_dec and not todays_tr:
            return
        old = self.lessons()
        prompt = build_review_prompt(day, todays_tr, todays_dec, bars, old.get("lessons"),
                                     scorecard(self.book.trades()), old.get("strategies"))
        r = self.todays_research(now)
        if r and r.get("picks"):
            moves = []
            for p in r["picks"]:
                b = bars.get(p["ticker"]) or []
                mv = f"{(b[-1]['c'] / b[0]['o'] - 1) * 100:+.2f}% open to now" if b else "no price data"
                tag = "" if p["ticker"] in self.watchlist() else " (watch-only, not tradable)"
                moves.append(f"- {p['ticker']}{tag}: {p.get('catalyst', '')} -> {mv}")
            prompt += ("\nThis morning's research picks and how they did (judge the research too; add a lesson if "
                       "a kind of pick keeps working or failing):\n" + "\n".join(moves) + "\n")
        facts = self.lab_facts(day)
        credit = self.rule_credit()
        if credit:
            prompt += ("\nRULE CREDIT (from the record: decisions cite rule ids; trades = closed buys that cited the rule):\n"
                       + "\n".join(f"- {k}: cited {c['cited']}x ({c['passes']} passes), {c['trades']} closed trades, "
                                    f"{c['wins']} wins, {c['pnl']:+.2f}, over {c['days']} day(s)"
                                    for k, c in sorted(credit.items(), key=lambda kv: int(kv[0][1:]))) + "\n")
        if facts:
            prompt += ("\nMEASURED FACTS from the trade lab (computed from the 1-minute bars, not opinions). Base stop "
                       "and re-entry lessons on these numbers; a lesson that contradicts them needs a stated reason:\n"
                       + facts + "\n")
        rec = {"t": now, "sym": "-", "trigger": "end-of-day review", "reason": "daily self-review"}
        rec["v"] = self.versions()
        try:
            rv = self.brain.review(prompt)
            rec["usage"] = self.call_usage()
            self.archive_prompt(rec, prompt, json.dumps(rv)[:20000])
            self.note_claude_result(True)
        except Exception as e:
            self.note_claude_result(False, str(e), now)
            rec.update(action="error", result=f"review failed: {e}"[:300])
            self.record(rec)
            return
        credit = self.rule_credit()
        last_id = assign_rule_ids(rv["lessons"], old.get("lessons") or [], old.get("last_rule_id", 0))
        for l in rv["lessons"]:                              # "solid" must be earned in the record, not claimed
            c = credit.get(l["id"]) or {}
            if l["status"] == "solid" and not (c.get("trades", 0) >= SOLID_MIN_TRADES and c.get("days", 0) >= SOLID_MIN_DAYS):
                l["status"] = "tentative"
                l["evidence"] = (l["evidence"] + f" [code: kept tentative - cited on {c.get('trades', 0)} closed trades "
                                 f"over {c.get('days', 0)} day(s); solid needs {SOLID_MIN_TRADES} over {SOLID_MIN_DAYS}]")[:500]
        new = {"day": day, "t": now, "summary": rv["summary"], "lessons": rv["lessons"], "last_rule_id": last_id,
               "strategies": rv.get("strategies", [])}
        write_json(self.lessons_path, new, indent=1)
        with open(self.lessons_hist, "a", encoding="utf-8") as f:
            f.write(json.dumps(new) + "\n")
        rec.update(action="review", why=rv["summary"], result=f"playbook now has {len(rv['lessons'])} rules")
        self.record(rec)

    # ---------- phone alerts (10-31, notify.py) ----------
    def alert(self, event: str, title: str, body: str) -> None:
        """Hand one alert to the notifier. Wrapped so an alert problem can never touch trading."""
        if self.notifier is None:
            return
        try:
            self.notifier.send(event, title, body)
        except Exception as e:
            log.warning("alert %s failed: %s", event, e)

    def alerts_status(self) -> dict:
        if self.notifier is None:
            return {"enabled": False, "set_up": False}
        try:
            return {"set_up": True, **self.notifier.status()}
        except Exception as e:
            return {"enabled": False, "set_up": False, "last_error": str(e)[:200]}

    def alert_buy(self, sym: str, setup: str, why: str) -> None:
        try:
            p = self.book.s["positions"][sym]
            self.alert("buy", f"BUY {p['shares']} {sym} @ {p['entry']:.2f}",
                       f"Bought {p['shares']} {sym} @ {p['entry']:.2f} | stop {p['stop']:.2f} | "
                       f"target {p['target']:.2f} | {setup} | {str(why)[:120]}")
        except Exception as e:
            log.warning("buy alert failed: %s", e)

    def alert_research(self, picks: list[dict], added: list[str], preview: bool = False) -> None:
        try:
            high = [p["ticker"] for p in picks if p.get("conviction") == "high"]
            rest = [p["ticker"] for p in picks if p["ticker"] not in high]
            self.alert("research", f"Research {'preview' if preview else 'done'}: {len(picks)} picks",
                       f"High: {', '.join(high) or 'none'} | Others: {', '.join(rest) or 'none'}"
                       + (f" | auto-added: {', '.join(added)}" if added else ""))
        except Exception as e:
            log.warning("research alert failed: %s", e)

    def alert_breaker(self, day: str) -> None:
        """The daily loss limit just started blocking buys: one loud alert per day."""
        try:
            if self.book.s.get("breaker_alert_day") == day:
                return
            with self.book.lock:
                self.book.s["breaker_alert_day"] = day
                self.book.save()
            eq = self.book.equity(self.prices)
            self.alert("breaker", "Daily loss limit hit: no new buys today",
                       f"Down {eq - self.book.s['day_start_equity']:+.2f} today (limit "
                       f"{self.cfg.get('max_daily_loss_pct')}%). Open positions and stops are still managed.")
        except Exception as e:
            log.warning("breaker alert failed: %s", e)

    def alert_trade(self, tr: dict, kind: str) -> None:
        """One alert for a closed trade: 'stop', 'target' or 'sell' (a Claude sell)."""
        try:
            word = {"stop": "STOP", "target": "TARGET", "sell": "SELL"}[kind]
            self.alert(kind, f"{word} {tr['sym']} {tr['pnl']:+.2f}",
                       f"Sold {tr['shares']} {tr['sym']} @ {tr['exit']:.2f} (bought {tr['entry']:.2f}) | "
                       f"P&L {tr['pnl']:+.2f} ({tr['pnl_pct']:+.2f}%) | held {round((tr['exit_t'] - tr['entry_t']) / 60)} min"
                       f" | {str(tr.get('why_out', ''))[:120]}")
        except Exception as e:
            log.warning("trade alert failed: %s", e)

    def maybe_recap(self, day: str, now: float) -> None:
        """Once a day at alerts.recap_minute (3:50 PM). The end-of-day review starts at 3:46 and runs on this same
        loop, so by the time this runs the review is finished and its headline can go in. If the desk started late
        (e.g. 3:55), the review runs first in the same pass and the recap goes right after it."""
        try:
            a = self.cfg.get("alerts") or {}
            if self.notifier is None or minutes_et(now) < a.get("recap_minute", 950):
                return
            if self.book.s.get("recap_day") == day:
                return
            with self.book.lock:
                self.book.s["recap_day"] = day           # mark first: never a loop of recaps
                self.book.save()
            start = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ET).timestamp()
            todays = [t for t in self.book.trades() if t["exit_t"] >= start]
            won = sum(1 for t in todays if t["pnl"] > 0)
            eq = self.book.equity(self.prices)
            pnl = eq - (self.book.s.get("day_start_equity") or eq)
            pg = self.progress(now)
            body = (f"P&L today {pnl:+.2f} | {len(todays)} trades ({won} won) | account ${eq:,.2f} | "
                    f"{pg['read_name']}: {pg['closed']}/{pg['next_read'] or pg['closed']}")
            L = self.lessons()
            if L.get("day") == day and L.get("summary"):
                head = re.split(r"(?<=[.!?])\s", str(L["summary"]).strip(), maxsplit=1)[0]
                body += " | Review: " + head[:160]
            elif self.brain is not None:
                body += " | Review: not ready"
            self.alert("recap", f"Day Desk recap {pnl:+.2f}", body)
        except Exception as e:
            log.warning("recap alert failed: %s", e)

    # ---------- decision log ----------
    def _load_recent(self) -> list[dict]:
        out = []
        if os.path.exists(self.dec_path):
            with open(self.dec_path, encoding="utf-8") as f:
                for line in f.readlines()[-200:]:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        return out

    def earlier_today(self, sym: str, now: float, price: float | None) -> str:
        """One line for the decision prompt (10-31): what already happened in this stock today, e.g. a stop-out
        and how price moved since. The 10-30 review wanted 'no quick re-entry after a stop' but the decision
        prompt never told Claude a stop had happened. Never lets a lab error block a trading call."""
        try:
            import lab
            return lab.symbol_today(self.folder, sym, et_now(now).date().isoformat(), now, last_price=price)
        except Exception as e:
            log.warning("earlier-today note failed for %s: %s", sym, e)
            return ""

    def refresh_lab(self, day: str) -> None:
        """Once a day at the close-out, after the bars are archived: rebuild data/lab_report.json for the page."""
        if self.book.s.get("lab_day") == day:
            return
        with self.book.lock:
            self.book.s["lab_day"] = day                 # mark first: a failure never retries in a loop
            self.book.save()
        for name in ("lab", "decision_score", "trigger_lab"):     # each zero Claude calls; each failure isolated
            try:
                __import__(name).write_report(self.folder)
            except Exception as e:
                log.warning("%s refresh failed: %s", name, e)

    def lab_summary(self) -> dict | None:
        """The page's trade-lab panel: the summary parts of data/lab_report.json (cached on the file's mtime)."""
        path = os.path.join(self.folder, "data", "lab_report.json")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        if getattr(self, "_lab_cache", (None,))[0] != mt:
            try:
                with open(path, encoding="utf-8") as f:
                    r = json.load(f)
                keep = ("n_trades", "n_with_bars", "stop_multipliers", "stopped_then_recovered",
                        "reentry_after_stop", "updated_t")
                self._lab_cache = (mt, {k: r.get(k) for k in keep})
            except (OSError, ValueError):
                return None
        out = dict(self._lab_cache[1])
        out["decisions"] = self._small_json("decision_score.json", ("header", "n", "days", "by_action",
                                            "buy_minus_pass", "dodged_losers", "missed_winners"))
        tl = self._small_json("trigger_lab.json", ("days", "rows"))
        if tl:
            tl["rows"] = [{k: r.get(k) for k in ("trigger", "fires", "per_day", "mean_r", "random_mean_r", "lift",
                                                 "ci_low", "ci_high", "verdict")} for r in tl.get("rows") or []]
            tl["days"] = len(tl.get("days") or [])
        out["triggers"] = tl
        return out

    def _small_json(self, name: str, keep: tuple) -> dict | None:
        """Only the summary keys of a (possibly large) report, cached on mtime, for the page's 5-second polling."""
        path = os.path.join(self.folder, "data", name)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        cache = getattr(self, "_json_cache", {})
        if cache.get(name, (None,))[0] != mt:
            try:
                with open(path, encoding="utf-8") as f:
                    r = json.load(f)
                cache[name] = (mt, {k: r.get(k) for k in keep})
                self._json_cache = cache
            except (OSError, ValueError):
                return None
        return dict(cache[name][1])

    def lab_facts(self, day: str) -> str:
        """Measured facts about today's trades for the end-of-day review (trade lab, bars on disk, no Claude)."""
        try:
            import lab
            facts = lab.review_facts(self.folder, day)
        except Exception as e:
            log.warning("lab facts failed for %s: %s", day, e)
            facts = ""
        try:                                             # hindsight score of every buy AND pass (10-31); never blocks
            import decision_score
            extra = decision_score.review_lines(self.folder, day)
        except Exception as e:
            log.warning("decision score failed for %s: %s", day, e)
            extra = ""
        try:                                             # which scanner triggers earn a call (10-31); never blocks
            import trigger_lab
            trig = trigger_lab.summary_lines(self.folder) if trigger_lab.archive_days(self.folder) else ""
        except Exception as e:
            log.warning("trigger lab summary failed: %s", e)
            trig = ""
        return "\n".join(x for x in (facts, extra, trig) if x)

    # ---------- base rates for every decision (10-31 weekend) ----------
    # Before this, the desk's own measurements (trigger lab, decision score) reached Claude only in the end-of-day
    # review, i.e. AFTER the day's decisions. The 21-day trigger lab found no trigger with a mean R clearly above 0
    # (the scanner is an alarm clock, not an edge), and the decision score found Claude's buys scoring below its
    # passes. Putting that in front of every fresh decision is a prompt change (part of the tested system, pre-reg).
    # Read from the two JSON reports rebuilt at each close-out; cached on their mtimes; never blocks a call.
    def base_rates(self, kind: str, held: bool) -> str:
        if held or not self.cfg.get("base_rates_in_prompt", True):
            return ""
        D = os.path.join(self.folder, "data")
        key = []
        for n in ("trigger_lab.json", "decision_score.json"):
            p = os.path.join(D, n)
            key.append(os.path.getmtime(p) if os.path.exists(p) else 0)
        cache = getattr(self, "_br_cache", None)
        if not cache or cache[0] != key:
            tl = ds = None
            try:
                tl = json.load(open(os.path.join(D, "trigger_lab.json"), encoding="utf-8")) if key[0] else None
            except Exception as e:
                log.warning("base rates: trigger lab unreadable: %s", e)
            try:
                ds = json.load(open(os.path.join(D, "decision_score.json"), encoding="utf-8")) if key[1] else None
            except Exception as e:
                log.warning("base rates: decision score unreadable: %s", e)
            cache = (key, tl, ds)
            self._br_cache = cache
        _, tl, ds = cache
        lines = []
        row = next((r for r in (tl or {}).get("rows", []) if r.get("trigger") == kind), None)
        if row and row.get("mean_r") is not None and row.get("random_mean_r") is not None:
            lines.append(f"- This wake-up trigger ({kind}) replayed on {row.get('days', '?')} saved days, {row.get('n_r', '?')} "
                         f"times, with one standard trade (stop 2 typical 1-minute ranges, target 1.5x risk): average "
                         f"{row['mean_r']:+.2f} R; random minutes on the same stocks {row['random_mean_r']:+.2f} R "
                         f"({row.get('verdict', '')}). The trigger alone is not an edge; a buy needs a reason beyond it.")
        ba = (ds or {}).get("by_action") or {}
        if ba.get("buy", {}).get("n") and ba.get("pass", {}).get("n"):
            b, p = ba["buy"], ba["pass"]
            lines.append(f"- Your own judgment so far ({ds.get('n', '?')} decisions over {len(ds.get('days') or [])} days, "
                         f"same standard trade): your buys averaged {b['mean_r']:+.2f} R, your passes {p['mean_r']:+.2f} R "
                         f"({(ds.get('buy_minus_pass') or {}).get('verdict', '')}). If your buys keep scoring below your "
                         f"passes, your filter is not adding anything yet.")
        if not lines:
            return ""
        return ("Base rates (the desk's own hindsight measurements, small samples, not rules):\n" + "\n".join(lines) +
                "\nIf you buy, say in 'why' what makes THIS case better than the usual one.")

    def versions(self) -> dict:
        """What produced a decision (10-31, idea from ai-hedge-fund signals/llm_agent.py + llm/contract.py): short
        hashes of the trading instructions, the playbook and the scanner settings. Stamped on every Claude call so
        results can later be split by version (descriptive only) and any call traced to its exact inputs."""
        import brain
        h = lambda s: hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
        L = self.lessons()
        return {"sys": h(brain.SYSTEM), "playbook": f"{L.get('day') or 'none'}:{h(json.dumps(L.get('lessons') or []))}",
                "scanner": h(json.dumps(self.cfg.get("scanner") or {}, sort_keys=True)),
                "lean": h(json.dumps(self.cfg.get("claude_lean") or {}, sort_keys=True))}

    def archive_prompt(self, rec: dict, prompt: str, reply: str | None) -> None:
        """Keep the exact prompt and Claude's raw reply for every trading/review call in data/prompts/<day>.jsonl
        (a few KB each). The id lands in decisions.jsonl as prompt_id. Never lets a write error touch trading."""
        try:
            day = et_now(rec["t"]).date().isoformat()
            folder = os.path.join(self.folder, "data", "prompts")
            os.makedirs(folder, exist_ok=True)
            pid = f"{int(rec['t'])}-{rec.get('sym', '-')}-{secrets.token_hex(2)}"
            with open(os.path.join(folder, day + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": pid, "t": rec["t"], "sym": rec.get("sym"), "trigger": rec.get("trigger"),
                                    "v": rec.get("v"), "prompt": prompt, "reply": reply}) + "\n")
            rec["prompt_id"] = pid
        except Exception as e:
            log.warning("could not archive prompt: %s", e)

    def fresh_price(self, sym: str, now: float, t_ask: float) -> tuple[float | None, float, str | None]:
        """The price AFTER Claude answered (10-31; freqtrade refreshes the rate at order time, Lean refuses to fill
        on stale data). A call takes 5-150 s; filling at the pre-call price gave every momentum entry a free look
        back. Returns (price, the desk-clock time it applies to, refusal reason or None). Runs in the trading loop
        thread, so yfinance stays single-threaded."""
        when = now + max(0.0, time.time() - t_ask)
        try:
            b = (self.feed.bars_1m([sym]) or {}).get(sym) or []
        except Exception as e:
            return None, when, f"couldn't re-check the price after Claude answered ({e})"[:200]
        if not b:
            return None, when, "no price after Claude answered"
        age = when - b[-1]["t"]
        if age > self.cfg["max_fill_staleness_s"]:
            return None, when, f"price is {int(age)} s old after Claude answered (limit {self.cfg['max_fill_staleness_s']} s)"
        self.prices[sym] = b[-1]["c"]
        return b[-1]["c"], when, None

    def call_usage(self) -> dict | None:
        """Tokens and API-equivalent cost of the Claude call this thread just made (None if not reported)."""
        fn = getattr(self.brain, "last_usage", None)
        try:
            return fn() if fn else None
        except Exception:
            return None

    def usage_summary(self) -> dict:
        """Claude usage per Eastern day from decisions.jsonl: calls, tokens and API-equivalent dollars, split by
        kind. Cached on the file's size+mtime so the page's polling doesn't re-read it every few seconds.
        Calls logged before 10-31 have no usage and are counted as 'unmeasured'."""
        try:
            st = os.stat(self.dec_path)
        except OSError:
            return {"days": [], "measured_since": None}
        key = (st.st_size, st.st_mtime)
        if getattr(self, "_usage_cache", (None,))[0] == key:
            return self._usage_cache[1]
        days: dict[str, dict] = {}
        first = None
        with open(self.dec_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                trig, act = r.get("trigger", ""), r.get("action", "")
                if trig in NOT_CLAUDE_CALLS or act == "-":
                    continue                              # engine events and skipped asks: no Claude call made
                kind = {"morning research": "research", "end-of-day review": "review",
                        "self-test": "self-test"}.get(trig, "trading")
                day = et_now(r.get("t", 0)).date().isoformat()
                d = days.setdefault(day, {"day": day, "calls": 0, "unmeasured": 0, "cost_usd": 0.0,
                                          "tok_in": 0, "tok_out": 0, "by_kind": {}})
                k = d["by_kind"].setdefault(kind, {"calls": 0, "cost_usd": 0.0})
                d["calls"] += 1
                k["calls"] += 1
                u = r.get("usage")
                if not u:
                    d["unmeasured"] += 1
                    continue
                first = first or day
                d["cost_usd"] = round(d["cost_usd"] + u.get("cost_usd", 0), 4)
                k["cost_usd"] = round(k["cost_usd"] + u.get("cost_usd", 0), 4)
                d["tok_in"] += u.get("tok_in", 0)
                d["tok_out"] += u.get("tok_out", 0)
        out = {"days": sorted(days.values(), key=lambda d: d["day"])[-7:], "measured_since": first}
        self._usage_cache = (key, out)
        return out

    def progress(self, now: float) -> dict:
        """Where the pre-registered test stands (claude/day-desk-preregistration.md PART 2). PROGRESS ONLY: the
        grade itself is grade_read.py's job, and it refuses to give a verdict before 60 trades. Trades per day
        counts every Eastern weekday the desk made a trading call or closed a trade, so quiet days slow the rate."""
        trades = self.book.trades()
        closed = len(trades)
        days = {et_now(t["exit_t"]).date().isoformat() for t in trades}
        for r in self.recent:
            if r.get("trigger") not in NOT_CLAUDE_CALLS and r.get("action") not in (None, "-", "self-test",
                                                                                     "research", "review"):
                days.add(et_now(r["t"]).date().isoformat())
        per_day = round(closed / len(days), 2) if days else 0.0
        reads = self.cfg.get("grade_reads", [60, 120])
        nxt = next((n for n in reads if closed < n), None)
        left = (nxt - closed) if nxt else 0
        graded = 0
        if os.path.exists(self.pick_results_path):
            with open(self.pick_results_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if str(r.get("conviction", "")).lower() == "high" and r.get("vs_qqq_pct") is not None:
                        graded += 1
        return {"closed": closed, "next_read": nxt, "read_name": f"Read {reads.index(nxt) + 1}" if nxt else "done",
                "trades_left": left, "trading_days": len(days), "per_day": per_day,
                "est_days_left": (round(left / per_day) if per_day else None) if nxt else 0,
                "research_graded": graded, "research_needed": self.cfg.get("research_grade_at", 40)}

    def record(self, rec: dict) -> None:
        with open(self.dec_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self.recent.append(rec)
        self.recent = self.recent[-200:]
        log.info("%s %s: %s -> %s %s", hm(rec["t"]), rec.get("sym", ""), rec.get("trigger", ""),
                 rec.get("action", ""), rec.get("result", ""))

    # ---------- one tick of the loop ----------
    def step(self, now: float) -> None:
        d = et_now(now)
        m = minutes_et(now)
        self.maybe_morning_research(now)
        if d.weekday() >= 5 or m < 9 * 60 + 30 or m >= 16 * 60 + 15:
            self.status["market"] = "closed"
            return
        watch = self.watchlist()
        held = list(self.book.s["positions"])
        shadow = self.watch_only_picks(now, watch)    # priced for grading the research, never traded
        syms = sorted(set(watch) | set(held) | set(shadow) | {"QQQ"})
        try:
            bars = self.feed.bars_1m(syms)
            self.status["feed_ok"], self.status["error"] = True, ""
        except Exception as e:                       # Yahoo hiccup: try again next tick
            self.status["feed_ok"], self.status["error"] = False, f"price feed failed: {e}"
            log.warning(self.status["error"])
            return
        self.status["last_poll_t"] = now
        if not bars.get("QQQ"):
            self.status["market"] = "no data (holiday or feed problem)"
            return
        self.status["market"] = "open"
        self.prices = {s: b[-1]["c"] for s, b in bars.items() if b}
        self.update_pick_moves(now, bars)
        for tr in self.book.new_day(d.date().isoformat(), self.prices):
            self.record({"t": now, "sym": tr["sym"], "trigger": "new day", "action": "sell",
                         "why": tr["why_out"], "result": f"sold, {tr['pnl']:+.2f}"})

        # 1) stops and targets, as resting orders, on every new 1-minute bar
        for sym in list(self.book.s["positions"]):
            tr = self.book.check_exits(sym, bars.get(sym, []), now)
            if tr and tr["why_out"].startswith("stop hit"):
                self.lock_after_stop(sym, tr, d)
            if tr:
                self.record({"t": tr["exit_t"], "sym": sym, "trigger": "resting order", "action": "sell",
                             "why": tr["why_out"], "result": f"sold {tr['shares']} @ {tr['exit']:.2f}, {tr['pnl']:+.2f}"})
                self.alert_trade(tr, "target" if str(tr["why_out"]).startswith("target") else "stop")

        # 2) end of day: everything out
        if m >= self.cfg["flatten_minute"]:
            for sym in list(self.book.s["positions"]):
                px = self.prices.get(sym) or self.book.s["positions"][sym]["last"]
                tr = self.book.sell(sym, px, now, "end-of-day flat")
                self.record({"t": now, "sym": sym, "trigger": "3:45 PM close-out", "action": "sell",
                             "why": "flat every night", "result": f"sold @ {tr['exit']:.2f}, {tr['pnl']:+.2f}"})
            self.status["market"] = "open (flat for the day)"
            self.score_picks(d.date().isoformat(), bars, now)
            self.archive_bars(d.date().isoformat(), bars)
            self.refresh_lab(d.date().isoformat())
            if m >= self.cfg["flatten_minute"] + 1:
                self.end_of_day_review(d.date().isoformat(), bars, now)
            self.maybe_recap(d.date().isoformat(), now)   # after the review, same pass (phone alert, 10-31)
            return
        why_not = (self.cfg.get("no_trade_days") or {}).get(d.date().isoformat())
        if why_not:
            self.status["market"] = f"open, but no trading today: {why_not}"
            return
        if self.book.s["paused"]:
            self.status["market"] = "open (paused by you)"
            return

        # 3) what is worth asking Claude about. The scanner and Claude see FINISHED minutes only (10-31): a
        # half-formed bar has a fraction of its volume (deflating volume ratios) and its close is not final, and
        # the trigger lab can only replay finished bars, so live and replay must agree.
        bars = {s: [b for b in bl if b["t"] + BAR_SECONDS <= now] for s, bl in bars.items()}
        asks: list[tuple[str, str, str]] = []
        qqq = bars.get("QQQ", [])
        no_new = self.book.day_loss_hit(self.prices)   # circuit breaker: don't spend calls on buys that would be refused
        if no_new:
            self.status["market"] = f"open (daily loss limit hit: no new buys today)"
            self.alert_breaker(d.date().isoformat())       # phone alert, first time only each day
        for sym, p in self.book.s["positions"].items():
            # protect-gain (10-31, freqtrade's trailing_stop_positive idea kept as Claude's JUDGEMENT, not an engine
            # rule): once a position is up protect_gain_at_r risk-units, ask once whether to raise the stop. Both AMD
            # trades on 10-30 were +0.8-0.9% before stopping out. Allowed scanner change; ~0-2 calls/day.
            risk = p["entry"] - p.get("stop0", p["stop"])
            last = (bars.get(sym) or [{}])[-1].get("c")
            if (risk > 0 and last and not p.get("gain_asked")
                    and last >= p["entry"] + self.cfg["protect_gain_at_r"] * risk):
                with self.book.lock:
                    p["gain_asked"] = True
                    self.book.save()
                asks.insert(0, (sym, "protect-gain", f"your position is up {(last / p['entry'] - 1) * 100:.2f}% "
                                f"= {(last - p['entry']) / risk:.1f}x the risk you took; decide whether to raise the stop"))
                continue
            if now - self.last_review.get(sym, p["entry_t"]) >= self.cfg["review_minutes"] * 60:
                asks.append((sym, "review", f"regular {self.cfg['review_minutes']}-minute check on your open position"))
        for sym in ([] if no_new else watch):
            for kind, reason in scan(sym, bars.get(sym, []), qqq, self.cfg):
                if self.cool.ready(sym, kind, now):
                    asks.append((sym, kind, reason))
                    break
        seen = set()
        for sym, kind, reason in asks:
            if sym in seen:
                continue
            seen.add(sym)
            if len(seen) > 2:                         # at most two questions per tick
                break
            self.ask(sym, kind, reason, bars.get(sym, []), qqq, now, watch)

    def ask(self, sym, kind, reason, bars, qqq, now, watch) -> None:
        self.cool.mark(sym, kind, now)
        self.last_review[sym] = now
        rec = {"t": now, "sym": sym, "trigger": kind, "reason": reason}
        if not bars:
            return
        if self.brain is None:
            rec.update(action="-", result="would ask Claude (install Claude Code to turn this on)")
            self.record(rec)
            return
        if self.book.s["calls_today"] >= self.cfg["max_calls_per_day"]:
            rec.update(action="-", result=f"skipped: daily cap of {self.cfg['max_calls_per_day']} Claude calls reached")
            self.record(rec)
            return
        price = bars[-1]["c"]
        rec["price"] = price
        with self.book.lock:
            view = {"cash": self.book.s["cash"], "positions": dict(self.book.s["positions"])}
            max_sh = self.book.max_shares(price)
            self.book.s["calls_today"] += 1
            self.book.save()
        prompt = build_prompt(sym, reason, bars, qqq, self.feed.news(sym), view, max_sh,
                              self.lessons().get("lessons"), scorecard(self.book.trades()),
                              self.todays_research(now), self.earlier_today(sym, now, price),
                              self.base_rates(kind, sym in view["positions"]))
        rec["v"] = self.versions()
        t_ask = time.time()
        try:
            dec, _raw = self.brain.decide(prompt)
            rec["usage"] = self.call_usage()
            self.archive_prompt(rec, prompt, _raw)
            self.note_claude_result(True)
        except Exception as e:
            self.note_claude_result(False, str(e), now)
            self.archive_prompt(rec, prompt, f"ERROR: {e}"[:500])
            rec.update(action="error", result=f"Claude call failed: {e}"[:300])
            self.record(rec)
            return
        rec.update(action=dec["action"], why=dec["why"], stop=dec.get("stop"), target=dec.get("target"),
                   rules=dec.get("rules") or [])
        if dec.get("p_win") is not None:
            rec["p_win"] = dec["p_win"]               # Claude's own odds, for decision_score.py calibration
        held = sym in self.book.s["positions"]
        act = dec["action"]
        if act in ("buy", "sell") and (act == "sell") == held:
            fill, fill_t, ferr = self.fresh_price(sym, now, t_ask)
            rec.update(ask_price=price, fill_price=fill, latency_s=round(fill_t - now, 1),
                       drift_bps=round((fill / price - 1) * 1e4, 1) if fill and price else None)
        if act == "buy" and not held:
            rec["setup"] = dec.get("setup") or "untagged"
            if ferr:
                rec["result"] = f"refused: {ferr}"
            else:
                shares = min(dec.get("shares") or max_sh, self.book.max_shares(fill))
                rec["shares"] = shares
                err = self.book.buy(sym, fill, shares, dec.get("stop") or 0, dec.get("target") or 0,
                                    dec["why"], fill_t, watch, rec["setup"])
                rec["result"] = f"refused: {err}" if err else f"bought {shares} @ {self.book.s['positions'][sym]['entry']:.2f}"
                if not err:
                    self.alert_buy(sym, rec["setup"], dec["why"])
        elif act == "sell" and held:
            tr = self.book.sell(sym, fill if not ferr else price, fill_t if not ferr else now, dec["why"])
            rec["result"] = f"sold @ {tr['exit']:.2f}, {tr['pnl']:+.2f}" + (f" (at the pre-answer price: {ferr})" if ferr else "")
            self.alert_trade(tr, "sell")
        elif act == "adjust" and held:
            err = self.book.adjust(sym, dec.get("stop"), dec.get("target"))
            rec["result"] = f"refused: {err}" if err else "stop/target moved"
        elif act in ("pass", "hold"):
            rec["result"] = "no trade" if not held else "holding"
        else:
            rec["result"] = f"ignored: '{act}' doesn't fit ({'held' if held else 'not held'})"
        self.record(rec)

    # ---------- what the page shows ----------
    def snapshot(self) -> dict:
        with self.book.lock:
            s = self.book.s
            positions = []
            for sym, p in s["positions"].items():
                last = self.prices.get(sym) or p.get("last") or p["entry"]
                positions.append({**p, "sym": sym, "last": last, "pnl": round((last - p["entry"]) * p["shares"], 2)})
            eq = self.book.equity(self.prices)
            trades = self.book.trades()
            return {
                "now": time.time(), "status": self.status, "watchlist": self.watchlist(),
                "cash": round(s["cash"], 2), "equity": eq, "start_cash": s["start_cash"],
                "day_start_equity": s["day_start_equity"], "paused": s["paused"],
                "calls_today": s["calls_today"], "max_calls": self.cfg["max_calls_per_day"],
                "model": self.cfg["model"], "positions": positions,
                "trades": trades[-300:][::-1], "decisions": self.recent[-80:][::-1],
                "learned": self.lessons(), "scorecard": scorecard(trades),
                "research": self.research(), "research_running": self.research_running,
                "auto_added": self.auto_added(), "pick_moves": self.pick_moves,
                "usage": self.usage_summary(), "alerts": self.alerts, "phone_alerts": self.alerts_status(),
                "locks": {sym: why for sym in self.watchlist() if (why := self.cool.locked(sym, time.time()))}, "progress": self.progress(time.time()), "lab": self.lab_summary(),
            }


# ---------------- web server ----------------
def make_handler(desk: Desk, key: str):
    page = os.path.join(desk.folder, "page.html")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):            # keep the console quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if key is None:                    # access key turned off in config.json
                return True
            got = self.headers.get("X-Key") or parse_qs(urlparse(self.path).query).get("key", [""])[0]
            return secrets.compare_digest(got.encode(), key.encode())

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                with open(page, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                if not self._authed():
                    return self._send(401, {"error": "wrong or missing access key"})
                return self._send(200, desk.snapshot())
            if path == "/api/ping":                     # proof of life, readable in a phone browser
                return self._send(200, desk.health.heartbeat())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._send(401, {"error": "wrong or missing access key"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad request"})
            path = urlparse(self.path).path
            if path == "/api/watchlist":
                err = desk.set_watchlist(body.get("symbols", []))
                return self._send(400 if err else 200, {"error": err} if err else {"ok": True})
            if path == "/api/pause":
                with desk.book.lock:
                    desk.book.s["paused"] = bool(body.get("paused"))
                    desk.book.save()
                return self._send(200, {"ok": True})
            if path == "/api/alert-test":               # the page's "Send test alert" button
                if desk.notifier is None:
                    return self._send(200, {"ok": False, "result": "Phone alerts are not set up in this run."})
                ok, msg = desk.notifier.test()
                return self._send(200, {"ok": ok, "result": msg})
            if path == "/api/research":
                return self._send(200, {"result": desk.run_research(time.time())})
            if path == "/api/flatten":
                now = time.time()
                for sym in list(desk.book.s["positions"]):
                    px = desk.prices.get(sym) or desk.book.s["positions"][sym]["last"]
                    tr = desk.book.sell(sym, px, now, "sold by you (Sell all button)")
                    desk.record({"t": now, "sym": sym, "trigger": "you", "action": "sell",
                                 "why": "Sell all button", "result": f"sold @ {tr['exit']:.2f}, {tr['pnl']:+.2f}"})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return H


def access_key(folder: str) -> str:
    p = os.path.join(folder, "data", "access_key.txt")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    k = secrets.token_hex(4)                   # 8 characters, easy to type on a phone once
    with open(p, "w", encoding="utf-8") as f:
        f.write(k)
    return k


def main() -> None:
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(os.path.join(HERE, "data", "day_desk.log"), encoding="utf-8")])
    cfg = load_config()
    from feed import YahooFeed
    feed = YahooFeed()
    exe = find_claude_cli()
    brain = None
    if exe:
        from brain import ClaudeCLIBrain
        brain = ClaudeCLIBrain(exe, cfg["model"], lean=cfg.get("claude_lean"))
    notifier = None
    try:
        from notify import Notifier
        notifier = Notifier(HERE, cfg)               # phone alerts through ntfy (10-31); its own sender thread
    except Exception as e:
        log.warning("phone alerts could not start: %s", e)
    desk = Desk(HERE, cfg, feed, brain, notifier)
    if not os.path.exists(desk.watch_path):
        desk.set_watchlist(desk.watchlist())
    akey = access_key(HERE) if cfg.get("require_access_key", False) else None

    desk.health.start()                        # data/heartbeat.json every 30 s + the data/inbox mailbox
    set_window_title()
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), make_handler(desk, akey))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("=" * 64)
    print(" DAY DESK is running. PAPER MONEY ONLY.")
    print(f" Phone (Tailscale):  http://<this PC's Tailscale name>:{cfg['port']}")
    print(f" This PC:            http://localhost:{cfg['port']}")
    print(f" Access key:         {akey + '   (the page asks for it once)' if akey else 'off (Tailscale-only firewall rule is the lock)'}")
    print(f" Claude:             {'ON via Claude Code (your subscription), model ' + cfg['model'] if brain else 'OFF - Claude Code not found, watching only'}")
    print(f" Phone alerts:       {('ON, ntfy topic in data/alerts_topic.txt (the page shows how to subscribe)' if notifier.enabled else 'OFF in config.json') if notifier else 'could not start (see the log)'}")
    print(f" Proof of life:      data\\heartbeat.json, or http://localhost:{cfg['port']}/api/ping")
    print(" Close this window to stop it.")
    print("=" * 64)

    while True:
        try:
            desk.step(time.time())
        except Exception as e:
            log.exception("loop error: %s", e)
            desk.status["error"] = f"loop error: {e}"
        try:
            desk.health.loop_done(time.time())    # marks the loop alive + runs any queued 'feed' self-test
        except Exception as e:
            log.warning("self-test error: %s", e)
        time.sleep(cfg["poll_seconds"] if desk.status.get("market", "").startswith("open") else 60)


if __name__ == "__main__":
    main()
