"""Proof of life for Day Desk (added 2026-10-29, tomahawkeer: "build something into day desk to have it
let you know its working, or build in a response of some kind for you to use to test it").

Three ways to tell whether the desk is alive, none of which needs anyone at the PC:

1. HEARTBEAT  data/heartbeat.json is rewritten every `heartbeat_seconds` by its own thread.
   - The file's age says whether the PROGRAM is alive (old file = RUN.bat is closed or crashed).
   - `loop_age_s` inside it says whether the TRADING LOOP is alive (the program can be up with the
     loop stuck, e.g. on a hung price download). Each verdict is written out in plain words in `verdict`.

2. MAILBOX    drop a text file with a NEW, never-used name into data/inbox/ holding one word:
                 ping    -> instant answer: the heartbeat plus the book
                 feed    -> the trading loop fetches prices from Yahoo right now and reports what came back
                 claude  -> one tiny Claude Code call, to prove the subscription login still works
                 help    -> this list
   The answer appears as data/outbox/<same name>.json, usually within `mailbox_check_seconds`.
   A name that already has an answer is never processed again, so never reuse a name.

3. URL        http://<PC>:8790/api/ping shows the heartbeat in a browser (works from the phone).

Nothing here can trade. Paper only, like everything else in this folder.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from engine import ET

VERSION = "2026-10-29 heartbeat+mailbox v1"
COMMANDS = {
    "ping": "instant answer: is the program alive, is the trading loop alive, the book",
    "feed": "the trading loop fetches Yahoo prices right now and reports what came back",
    "claude": "one tiny Claude Code call on your subscription, to prove the login works",
    "help": "this list",
}
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
log = logging.getLogger("daydesk")


# Windows refuses to swap a file in while anyone else has it open (Notepad, a reader thread, the copy tool):
# os.replace raises PermissionError [WinError 5]. Found 10-29 by TEST.bat on the desk PC (Linux never does this).
# So: retry for up to REPLACE_TRIES x REPLACE_WAIT_S = 2 s, which outlasts any normal read, then give up loudly.
REPLACE_TRIES = 40
REPLACE_WAIT_S = 0.05


def write_json(path: str, obj, indent: int | None = None) -> None:
    """Atomic write (temp file, then swap): a reader never sees half a file. Retries the swap on Windows."""
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent)
    for i in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == REPLACE_TRIES - 1:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(REPLACE_WAIT_S)


def et_str(t: float | None) -> str | None:
    if not t:
        return None
    return datetime.fromtimestamp(t, ET).strftime("%a %Y-%m-%d %I:%M:%S %p ET")


class Health:
    def __init__(self, desk, started_t: float | None = None):
        self.desk = desk
        cfg = desk.cfg
        self.beat_s = cfg.get("heartbeat_seconds", 30)
        self.check_s = cfg.get("mailbox_check_seconds", 10)
        self.stuck_s = cfg.get("loop_stuck_seconds", 600)
        self.claude_max = cfg.get("selftest_claude_max_per_day", 5)
        data = os.path.join(desk.folder, "data")
        self.hb_path = os.path.join(data, "heartbeat.json")
        self.inbox = os.path.join(data, "inbox")
        self.outbox = os.path.join(data, "outbox")
        os.makedirs(self.inbox, exist_ok=True)
        os.makedirs(self.outbox, exist_ok=True)
        self.started_t = started_t or time.time()
        self.loops = 0
        self.last_loop_t: float | None = None
        self.taken: set[str] = set()                # requests already picked up (answered or in progress)
        self.feed_jobs: list[tuple[str, float]] = []   # run by the trading loop, never by this thread
        self.claude_busy = False
        self.claude_today: dict[str, int] = {}
        self.problem_since: float | None = None      # when the verdict stopped being WORKING (phone alert)
        self.problem_alerted = False
        self.lock = threading.Lock()
        self._stop = threading.Event()

    # ---------- called by the trading loop ----------
    def loop_done(self, now: float) -> None:
        """The main loop calls this after every step, so `loop_age_s` measures the LOOP, not just the program."""
        self.loops += 1
        self.last_loop_t = now
        if self.loops == 1:
            self.write_heartbeat(now)                 # don't leave "STARTING" on disk for a whole beat
        self.run_feed_jobs(now)

    # ---------- heartbeat ----------
    def heartbeat(self, now: float | None = None) -> dict:
        now = now or time.time()
        d = self.desk
        st = d.status
        s = d.book.s
        loop_age = round(now - self.last_loop_t) if self.last_loop_t else None
        if self.last_loop_t is None:
            verdict = "STARTING: program is up, the trading loop has not finished its first pass yet"
        elif loop_age > self.stuck_s:
            verdict = f"LOOP STUCK: program is up but the trading loop last finished {loop_age // 60} min ago"
        elif st.get("feed_ok") is False:
            verdict = "WORKING, PRICE FEED FAILING: loop is running and retrying; " + (st.get("error") or "")
        else:
            verdict = "WORKING: program and trading loop are both alive"
        if getattr(d, "alerts", None):
            verdict += " | ATTENTION: " + " | ".join(d.alerts)
        r = d.research()
        today = datetime.fromtimestamp(now, ET).date().isoformat()
        research = (f"done for today, {len(r.get('picks') or [])} picks" if r.get("day") == today and r.get("status") == "done"
                    else f"today's run: {r.get('status')}" if r.get("day") == today
                    else "running now" if d.research_running else f"not run yet today (last: {r.get('day')})")
        return {
            "alive": True, "verdict": verdict, "version": VERSION,
            "t": now, "time_et": et_str(now), "pid": os.getpid(),
            "started_et": et_str(self.started_t), "uptime_min": round((now - self.started_t) / 60, 1),
            "loops": self.loops, "loop_last_done_et": et_str(self.last_loop_t), "loop_age_s": loop_age,
            "market": st.get("market"), "feed_ok": st.get("feed_ok"), "last_poll_et": et_str(st.get("last_poll_t")),
            "error": st.get("error", ""), "brain": st.get("brain"),
            "calls_today": s.get("calls_today"), "max_calls": d.cfg.get("max_calls_per_day"),
            "book_day": s.get("day"), "cash": round(s.get("cash", 0), 2),
            "open_positions": sorted(s.get("positions", {})), "watchlist": d.watchlist(),
            "research": research, "reviewed_day": s.get("reviewed_day"),
            "how_to_read": (f"This file is rewritten every {self.beat_s} s. If it is more than a few minutes old, "
                            "Day Desk is not running (RUN.bat closed or crashed)."),
        }

    def write_heartbeat(self, now: float | None = None) -> None:
        write_json(self.hb_path, self.heartbeat(now), indent=1)

    # ---------- mailbox ----------
    def _answer_path(self, name: str) -> str:
        return os.path.join(self.outbox, os.path.splitext(name)[0] + ".json")

    def _write_answer(self, name: str, cmd: str, received: float, ok: bool, summary: str, detail=None,
                      done: bool = True) -> None:
        write_json(self._answer_path(name), {
            "request": name, "command": cmd, "ok": ok, "done": done, "summary": summary,
            "received_et": et_str(received), "answered_et": et_str(time.time()),
            "detail": detail, "version": VERSION}, indent=1)

    def check_inbox(self, now: float | None = None) -> list[str]:
        """Pick up new requests. Returns the names it took (for tests)."""
        now = now or time.time()
        took = []
        try:
            names = sorted(os.listdir(self.inbox))
        except OSError:
            return took
        for name in names:
            path = os.path.join(self.inbox, name)
            if name in self.taken or not os.path.isfile(path) or name.endswith(".tmp"):
                continue
            self.taken.add(name)
            if not NAME_RE.match(name) or os.path.exists(self._answer_path(name)):
                continue                                 # odd name, or already answered: never twice
            try:
                text = open(path, encoding="utf-8", errors="replace").read(200)
            except OSError:
                self.taken.discard(name)                 # still being written: try next time
                continue
            words = text.strip().split()
            cmd = (words[0] if words else name.split("-")[0].split(".")[0]).lower()
            took.append(name)
            self.handle(name, cmd, now)
        return took

    def handle(self, name: str, cmd: str, now: float) -> None:
        log.info("self-test request %s: %s", name, cmd)
        if cmd == "ping":
            hb = self.heartbeat(now)
            self._write_answer(name, cmd, now, True, hb["verdict"], hb)
        elif cmd == "help":
            self._write_answer(name, cmd, now, True, "commands: " + ", ".join(COMMANDS), COMMANDS)
        elif cmd == "feed":
            with self.lock:
                self.feed_jobs.append((name, now))
            self._write_answer(name, cmd, now, True, "queued: the trading loop will fetch prices on its next pass",
                               None, done=False)
        elif cmd == "claude":
            self.start_claude_test(name, now)
        else:
            self._write_answer(name, cmd, now, False, f"unknown command '{cmd}'. Try: " + ", ".join(COMMANDS), COMMANDS)

    # feed: runs INSIDE the trading loop, because yfinance downloads are not safe to run two at once
    def run_feed_jobs(self, now: float) -> None:
        with self.lock:
            jobs, self.feed_jobs = self.feed_jobs, []
        for name, received in jobs:
            syms = sorted(set(self.desk.watchlist()) | {"QQQ"})
            t0 = time.time()
            try:
                bars = self.desk.feed.bars_1m(syms)
            except Exception as e:
                self._write_answer(name, "feed", received, False, f"Yahoo price feed FAILED: {e}"[:300])
                continue
            secs = round(time.time() - t0, 1)
            detail = {s: ({"bars_today": len(b), "last_bar_et": et_str(b[-1]["t"]), "last_price": b[-1]["c"]}
                          if b else {"bars_today": 0}) for s, b in bars.items()}
            got = sum(1 for b in bars.values() if b)
            if got:
                summary = f"Yahoo answered in {secs}s: today's bars for {got} of {len(syms)} symbols"
            else:
                summary = (f"Yahoo answered in {secs}s with no error, but no bars for today yet "
                           "(normal before 9:30 AM ET, on weekends and holidays)")
            self._write_answer(name, "feed", received, True, summary, detail)

    # claude: its own thread, so a slow call never holds up trading
    def start_claude_test(self, name: str, now: float) -> None:
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        brain = self.desk.brain
        if brain is None:
            return self._write_answer(name, "claude", now, False, "Claude Code was not found on this PC: watch-only mode")
        with self.lock:
            if self.claude_busy:
                return self._write_answer(name, "claude", now, False, "a Claude self-test is already running; try again in a minute")
            if self.claude_today.get(day, 0) >= self.claude_max:
                return self._write_answer(name, "claude", now, False,
                                          f"refused: already did {self.claude_max} Claude self-tests today "
                                          "(selftest_claude_max_per_day in config.json)")
            self.claude_busy = True
            self.claude_today[day] = self.claude_today.get(day, 0) + 1
        self._write_answer(name, "claude", now, True, "running: asking Claude (takes 10-60 s)", None, done=False)

        def work():
            t0 = time.time()
            rec = {"t": now, "sym": "-", "trigger": "self-test", "reason": f"mailbox request {name}"}
            try:
                reply = brain.ping()
                fn = getattr(brain, "last_usage", None)
                rec["usage"] = fn() if fn else None              # same thread, so this is this call's usage
                secs = round(time.time() - t0, 1)
                ok = "DAY DESK OK" in reply.upper()
                summary = (f"Claude answered in {secs}s through Claude Code on your subscription" if ok
                           else f"Claude answered in {secs}s but not with the expected words")
                self._write_answer(name, "claude", now, ok, summary, {"reply": reply.strip()[:300], "seconds": secs})
                rec.update(action="self-test", why=reply.strip()[:200], result=summary)
            except Exception as e:
                rec.update(action="error", result=f"self-test failed: {e}"[:300])
                try:
                    self._write_answer(name, "claude", now, False, f"Claude call FAILED: {e}"[:300])
                except Exception as e2:                  # e.g. outbox locked: still log the call below
                    log.warning("could not write self-test answer: %s", e2)
            finally:
                with self.lock:
                    self.claude_busy = False
            try:
                self.desk.record(rec)                    # every Claude call is logged (CLAUDE.md rule 4)
            except Exception as e:                       # never let logging kill the thread
                log.warning("could not log self-test: %s", e)

        threading.Thread(target=work, daemon=True).start()

    # ---------- the background thread ----------
    # ---------- phone alert when the desk is unwell (10-31) ----------
    def watch_problem(self, now: float) -> bool:
        """Runs on THIS thread (not the trading loop, which may be the thing that is stuck). If the verdict has not
        been plain WORKING for alerts.desk_problem_minutes (5), send one loud phone alert for that episode; it resets
        once the verdict is WORKING again. A failing price feed only counts while the market is open: after hours the
        loop stops fetching, so a stale 'feed failing' must not ring the phone all night. Returns True if it alerted."""
        verdict = self.heartbeat(now)["verdict"]
        market = str(self.desk.status.get("market") or "")
        bad = not verdict.startswith("WORKING:") or "ATTENTION" in verdict   # 10-31: banners ring too
        if verdict.startswith("WORKING, PRICE FEED FAILING") and market.startswith("closed"):
            bad = False
        if not bad:
            self.problem_since = None
            self.problem_alerted = False
            return False
        if self.problem_since is None:
            self.problem_since = now
            self.problem_alerted = False
        mins = (self.desk.cfg.get("alerts") or {}).get("desk_problem_minutes", 5)
        if self.problem_alerted or now - self.problem_since < mins * 60:
            return False
        self.problem_alerted = True
        mins_on = round((now - self.problem_since) / 60)
        if verdict.startswith("WORKING:") and "ATTENTION" in verdict:
            self.desk.alert("desk_problem", "Day Desk needs attention",
                            f"{verdict.split('ATTENTION:', 1)[1].strip()[:300]} (for {mins_on} min)")
        else:
            self.desk.alert("desk_problem", "Day Desk problem: " + verdict.split(":")[0],
                            f"{verdict[:200]} (for {mins_on} min). Check the RUN.bat window on the PC.")
        return True

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        self.check_inbox(now)
        try:
            self.watch_problem(now)
        except Exception as e:                           # an alert problem never stops the heartbeat
            log.warning("desk-problem alert check failed: %s", e)
        if now - getattr(self, "_last_beat", 0) >= self.beat_s:
            self.write_heartbeat(now)
            self._last_beat = now

    def start(self) -> None:
        def run():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:                   # the heartbeat must outlive any one bad pass
                    log.warning("heartbeat/mailbox error: %s", e)
                self._stop.wait(self.check_s)
        self.write_heartbeat()
        threading.Thread(target=run, daemon=True, name="heartbeat").start()

    def stop(self) -> None:
        self._stop.set()
