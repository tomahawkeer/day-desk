"""Phone push alerts for Day Desk (added 2026-10-31).

How it reaches the phone: ntfy (https://ntfy.sh). Free, no account, no API key, no bill. tomahawkeer installs the "ntfy" app,
taps +, and subscribes to the desk's topic (the page's Controls section shows it). The desk then sends a plain HTTP POST
of text to https://ntfy.sh/<topic> with three headers: Title, Priority (1-5) and Tags. Python's own urllib, nothing
installed. Ideas from freqtrade (rpc/webhook.py, per-event notification levels) and jesse (services/notifier.py,
a queue + background sender so a slow or dead notification service never holds up trading).

Rules this file keeps:
  * The topic is the only "password": anyone who knows it can read the alerts. It is made once with
    secrets.token_urlsafe(12), saved to data/alerts_topic.txt, and is never written to config.json or the log.
  * send() NEVER raises. A failure is retried (`tries`, with backoff), then logged; trading never notices.
  * At most one message every `min_seconds_between` seconds (later ones wait their turn in the queue) and the
    same message twice within `dedupe_seconds` is dropped.
  * Bodies carry trade facts only (symbol, shares, price, P&L, one-line why). No keys, no account numbers.
Paper only, like everything else in this folder.
"""
from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger("daydesk")

PRIORITY = {"loud": 5, "normal": 3, "silent": 2, "off": None}
TAGS = {"buy": "green_circle", "sell": "white_circle", "stop": "red_circle", "target": "dart",
        "breaker": "octagonal_sign", "desk_problem": "warning", "recap": "bar_chart", "research": "newspaper",
        "test": "bell"}
DEFAULTS = {"enabled": True, "server": "https://ntfy.sh", "events": {}, "min_seconds_between": 10,
            "dedupe_seconds": 60, "tries": 3, "retry_wait_seconds": 2, "timeout_seconds": 10, "max_queued": 30}
ASCII = {"—": "-", "–": "-", "−": "-", "→": "->", "·": "|", "…": "...",
         "‘": "'", "’": "'", "“": '"', "”": '"'}


def ascii_header(s: str) -> str:
    """HTTP headers must be plain ASCII (Python refuses anything else): swap the usual fancy characters."""
    s = "".join(ASCII.get(c, c) for c in str(s))
    return s.encode("ascii", "replace").decode("ascii").replace("\r", " ").replace("\n", " ")[:200]


def http_post(url: str, data: bytes, headers: dict, timeout: float) -> int:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


class Notifier:
    def __init__(self, folder: str, cfg: dict, post=None, clock=None, sleep=None, start_thread: bool = True):
        self.folder = folder
        a = dict(DEFAULTS, **(cfg.get("alerts") or {}))
        self.cfg = a
        self.enabled = bool(a.get("enabled"))
        self.server = str(a.get("server") or DEFAULTS["server"]).rstrip("/")
        self.post = post or http_post
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.q: queue.Queue = queue.Queue(maxsize=int(a["max_queued"]))
        self.lock = threading.Lock()
        self.recent: dict[tuple, float] = {}        # (event, title, body) -> when it was accepted, for dedupe
        self.last_sent_t = 0.0                      # when the last message went out (rate limit)
        self.sent = self.failed = self.dropped = 0
        self.last_ok_t: float | None = None
        self.last_error = ""
        self.topic = self._topic()
        if start_thread:
            threading.Thread(target=self._run, daemon=True, name="alerts").start()

    # ---------- the topic: made once, kept in data/, never in config or the log ----------
    def _topic(self) -> str:
        p = os.path.join(self.folder, "data", "alerts_topic.txt")
        try:
            if os.path.exists(p):
                t = open(p, encoding="utf-8").read().strip()
                if t:
                    return t
            os.makedirs(os.path.dirname(p), exist_ok=True)
            t = "daydesk-" + secrets.token_urlsafe(12)
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
            return t
        except Exception as e:                      # never stop the desk over a topic file
            log.warning("alerts: could not read/write the topic file: %s", e)
            return "daydesk-" + secrets.token_urlsafe(12)

    def level(self, event: str) -> str:
        if event == "test":
            return "normal"
        lv = str((self.cfg.get("events") or {}).get(event, "normal")).lower()
        return lv if lv in PRIORITY else "normal"

    # ---------- what the desk calls ----------
    def send(self, event: str, title: str, body: str) -> bool:
        """Queue one alert. Returns True if queued. Never raises."""
        try:
            if not self.enabled:
                return False
            pr = PRIORITY[self.level(event)]
            if pr is None:
                return False
            now = self.clock()
            key = (event, title, body)
            with self.lock:
                ds = float(self.cfg["dedupe_seconds"])
                self.recent = {k: t for k, t in self.recent.items() if now - t < ds}
                if key in self.recent:
                    self.dropped += 1
                    return False
                self.recent[key] = now
            self.q.put_nowait((event, title, body, pr))
            return True
        except queue.Full:
            self.dropped += 1
            log.warning("alerts: queue full, dropped a %s alert", event)
            return False
        except Exception as e:
            log.warning("alerts: could not queue a %s alert: %s", event, e)
            return False

    def test(self) -> tuple[bool, str]:
        """The page's 'Send test alert' button. Sends now (not queued) so the page can say whether it worked."""
        try:
            if not self.enabled:
                return False, "Alerts are off (alerts.enabled is false in config.json)."
            now = self.clock()
            with self.lock:
                if now - self.recent.get(("test",), 0) < float(self.cfg["min_seconds_between"]):
                    return False, "A test went out a moment ago. Wait a few seconds."
                self.recent[("test",)] = now
            ok = self._deliver("test", "Day Desk test alert",
                               "If you can read this on your phone, alerts work. Paper money only.", PRIORITY["normal"])
            return (True, "Test sent. It should be on your phone within a few seconds.") if ok else \
                   (False, "Could not reach the alert server: " + (self.last_error or "unknown error"))
        except Exception as e:
            return False, f"test failed: {e}"[:200]

    def status(self) -> dict:
        host = urlparse(self.server).netloc or self.server
        return {"enabled": self.enabled, "server": self.server, "host": host, "topic": self.topic,
                "subscribe_url": f"ntfy://{host}/{self.topic}", "web_url": f"{self.server}/{self.topic}",
                "levels": {e: self.level(e) for e in (self.cfg.get("events") or {})},
                "sent": self.sent, "failed": self.failed, "last_ok_t": self.last_ok_t, "last_error": self.last_error}

    # ---------- the sender ----------
    def _run(self) -> None:
        while True:
            try:
                self._process(self.q.get())
            except Exception as e:                  # the sender thread must outlive any one bad message
                log.warning("alerts: sender error: %s", e)

    def drain(self) -> None:
        """Send everything queued, on this thread (tests)."""
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            self._process(item)

    def _process(self, item) -> None:
        event, title, body, pr = item
        wait = self.last_sent_t + float(self.cfg["min_seconds_between"]) - self.clock()
        if wait > 0:
            self.sleep(wait)                        # rate limit: wait our turn, don't drop a stop alert
        self._deliver(event, title, body, pr)

    def _deliver(self, event: str, title: str, body: str, pr: int) -> bool:
        tries = max(1, int(self.cfg["tries"]))
        url = f"{self.server}/{self.topic}"
        headers = {"Title": ascii_header(title), "Priority": str(pr), "Tags": TAGS.get(event, "bell"),
                   "Content-Type": "text/plain; charset=utf-8"}
        err = ""
        for i in range(tries):
            try:
                self.post(url, str(body)[:1000].encode("utf-8"), headers, float(self.cfg["timeout_seconds"]))
                with self.lock:
                    self.sent += 1
                    self.last_sent_t = self.last_ok_t = self.clock()
                    self.last_error = ""
                return True
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:200]
                if i < tries - 1:
                    self.sleep(float(self.cfg["retry_wait_seconds"]) * (2 ** i))   # 2 s, 4 s, ...
        with self.lock:
            self.failed += 1
            self.last_sent_t = self.clock()
            self.last_error = err
        log.warning("alerts: %s alert not sent after %d tries: %s", event, tries, err)   # topic never logged
        return False
