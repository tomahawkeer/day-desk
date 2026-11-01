"""Tests for phone push alerts (notify.py + the hooks in day_desk.py / health.py). No network: every send is faked."""
import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import FakeBrain                                       # noqa: E402
from feed import FakeFeed                                         # noqa: E402
import day_desk                                                   # noqa: E402
import notify                                                     # noqa: E402
from test_day_desk import CFG, HERE, ts, bar, flat_bars, breakout_set   # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Recorder:
    """A fake ntfy server: records every POST, or fails the first `fail` of them."""

    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def __call__(self, url, data, headers, timeout):
        self.calls.append({"url": url, "body": data.decode("utf-8"), "headers": headers})
        if len(self.calls) <= self.fail:
            raise OSError("network down")
        return 200


def make_notifier(tmp_path, post=None, events=None, clock=None, **extra):
    cfg = dict(CFG)
    cfg["alerts"] = dict(CFG["alerts"], **extra)
    if events is not None:
        cfg["alerts"]["events"] = events
    sleeps = []
    clock = clock or Clock()

    def sleep(s):
        sleeps.append(s)
        clock.t += s
    n = notify.Notifier(str(tmp_path), cfg, post=post or Recorder(), clock=clock, sleep=sleep, start_thread=False)
    return n, sleeps


class FakeNotifier:
    def __init__(self, boom=False):
        self.sent, self.boom = [], boom

    def send(self, event, title, body):
        if self.boom:
            raise RuntimeError("notifier exploded")
        self.sent.append((event, title, body))
        return True

    def events(self):
        return [e for e, _, _ in self.sent]


def desk_with(tmp_path, bars, answers, notifier, research=False):
    cfg = CFG if research else dict(CFG, research_minute=10 ** 6)
    brain = FakeBrain(answers)
    d = day_desk.Desk(str(tmp_path), cfg, FakeFeed(bars=bars), brain, notifier)
    d.set_watchlist(["AMD"])
    return d, brain


# ---------------- notify.py ----------------
def test_config_has_the_alerts_block_with_its_comment():
    a = CFG["alerts"]
    assert "_alerts" in CFG and all(w in CFG["_alerts"] for w in ("loud", "normal", "silent", "off"))
    assert a["events"] == {"buy": "silent", "sell": "silent", "stop": "loud", "target": "loud", "breaker": "loud",
                           "desk_problem": "loud", "recap": "normal", "research": "silent",
                           "token_ceiling": "loud"}     # 11-01: token ceiling guard
    assert a["server"] == "https://ntfy.sh" and "topic" not in json.dumps(a).lower().replace("_alerts", "")


def test_level_mapping_including_off(tmp_path):
    post = Recorder()
    n, _ = make_notifier(tmp_path, post, events={"stop": "loud", "recap": "normal", "buy": "silent", "sell": "off"})
    assert n.send("stop", "STOP AMD", "a") and n.send("recap", "Recap", "b") and n.send("buy", "BUY", "c")
    assert n.send("sell", "SELL", "d") is False                   # off = not sent at all
    assert n.send("brand_new_event", "x", "e")                     # unknown event = normal, never silently lost
    n.drain()
    assert [c["headers"]["Priority"] for c in post.calls] == ["5", "3", "2", "3"]
    assert all(c["url"] == "https://ntfy.sh/" + n.topic for c in post.calls)
    assert post.calls[0]["headers"]["Tags"] == "red_circle" and post.calls[0]["headers"]["Title"] == "STOP AMD"


def test_disabled_sends_nothing(tmp_path):
    post = Recorder()
    n, _ = make_notifier(tmp_path, post, enabled=False)
    assert n.send("stop", "x", "y") is False and n.test()[0] is False
    n.drain()
    assert post.calls == []


def test_topic_created_once_and_reused(tmp_path):
    n1, _ = make_notifier(tmp_path)
    n2, _ = make_notifier(tmp_path)
    assert n1.topic == n2.topic and n1.topic.startswith("daydesk-") and len(n1.topic) == 8 + 16
    assert open(tmp_path / "data" / "alerts_topic.txt").read().strip() == n1.topic
    assert n1.topic not in json.dumps(CFG)
    st = n1.status()
    assert st["subscribe_url"] == f"ntfy://ntfy.sh/{n1.topic}" and st["web_url"] == f"https://ntfy.sh/{n1.topic}"


def test_failure_is_retried_logged_and_never_raises(tmp_path, caplog):
    post = Recorder(fail=99)
    n, sleeps = make_notifier(tmp_path, post)
    assert n.send("stop", "STOP AMD", "body") is True
    n.drain()                                                      # must not raise
    assert len(post.calls) == 3 and sleeps == [2, 4]               # 3 tries, backoff 2 s then 4 s
    assert n.failed == 1 and "network down" in n.last_error
    assert "alert not sent after 3 tries" in caplog.text and n.topic not in caplog.text
    # a recovery: the next message goes through on its second try
    post2 = Recorder(fail=1)
    n.post = post2
    n.send("stop", "STOP NVDA", "body2")
    n.drain()
    assert len(post2.calls) == 2 and n.sent == 1 and n.last_error == ""


def test_send_never_raises_even_on_garbage(tmp_path):
    n, _ = make_notifier(tmp_path)
    n.q = None                                                     # any internal breakage
    assert n.send("stop", "x", "y") is False


def test_rate_limit_and_dedupe(tmp_path):
    clock = Clock(1000.0)
    post = Recorder()
    n, sleeps = make_notifier(tmp_path, post, clock=clock)
    assert n.send("stop", "STOP AMD", "same") is True
    assert n.send("stop", "STOP AMD", "same") is False             # duplicate within 60 s: dropped
    assert n.send("target", "TARGET NVDA", "other") is True
    n.drain()
    assert len(post.calls) == 2 and sleeps == [10]                 # second one waited its 10 s turn
    clock.t += 61
    assert n.send("stop", "STOP AMD", "same") is True              # after 60 s the same text may go again


def test_headers_are_ascii_safe(tmp_path):
    post = Recorder()
    n, _ = make_notifier(tmp_path, post)
    n.send("recap", "Day Desk recap −$5 → done — ok ☃", "P&L −$5 · fine")
    n.drain()
    t = post.calls[0]["headers"]["Title"]
    t.encode("ascii")
    assert t.startswith("Day Desk recap -$5 -> done - ok") and "−" in post.calls[0]["body"]


# ---------------- hooks in day_desk.py ----------------
def test_buy_fires_a_buy_alert(tmp_path):
    bars = breakout_set()
    fn = FakeNotifier()
    d, _ = desk_with(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0,
                                       "why": "breakout holding", "setup": "breakout"}], fn)
    d.step(bars["AMD"][-1]["t"] + 65)
    assert fn.events() == ["buy"]
    ev, title, body = fn.sent[0]
    assert title.startswith("BUY 10 AMD @ 100.") and "stop 99.00" in body and "breakout holding" in body


def test_claude_sell_fires_a_sell_alert(tmp_path):
    bars = breakout_set()
    fn = FakeNotifier()
    d, _ = desk_with(tmp_path, bars, [{"action": "sell", "why": "fading"}], fn)
    d.book.s["day"] = "2026-10-28"
    assert d.book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(9, 35), ["AMD"]) is None
    d.step(bars["AMD"][-1]["t"] + 65)                               # 30-minute review -> Claude says sell
    assert "sell" in fn.events() and "fading" in fn.sent[-1][2]


@pytest.mark.parametrize("low,high,event", [(97.0, 100.5, "stop"), (99.5, 105.0, "target")])
def test_resting_stop_and_target_fire_their_alerts(tmp_path, low, high, event):
    t = ts(11, 1)
    amd = flat_bars(n=60) + [bar(t, 100.0, high, low, 100.0)]
    bars = {"AMD": amd, "QQQ": flat_bars(n=61, price=500.0)}
    fn = FakeNotifier()
    d, _ = desk_with(tmp_path, bars, [], fn)
    d.book.s["day"] = "2026-10-28"
    d.book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(11, 0), ["AMD"])
    d.step(t + 30)
    assert not d.book.s["positions"] and fn.events()[0] == event
    assert fn.sent[0][1].startswith(event.upper() + " AMD") and "Sold 10 AMD" in fn.sent[0][2]


def test_breaker_alerts_once_a_day(tmp_path):
    bars = breakout_set()
    fn = FakeNotifier()
    d, brain = desk_with(tmp_path, bars, [], fn)
    d.book.s["cash"] = d.book.s["day_start_equity"] * (1 - CFG["max_daily_loss_pct"] / 100) - 10
    d.book.s["day"] = "2026-10-28"
    d.step(bars["AMD"][-1]["t"] + 65)
    d.step(bars["AMD"][-1]["t"] + 35)
    assert fn.events() == ["breaker"] and "no new buys" in fn.sent[0][1]


def test_recap_once_a_day_after_the_review(tmp_path):
    bars = breakout_set()
    fn = FakeNotifier()
    d, brain = desk_with(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "b"}], fn)
    brain.review_answer = {"summary": "Chased one breakout that failed. More detail here.", "lessons": [
        {"rule": "wait for a close", "evidence": "AMD", "status": "tentative"}]}
    d.step(bars["AMD"][-1]["t"] + 65)                               # buys
    late = {"AMD": bars["AMD"] + flat_bars(start_h=15, start_m=44, n=3),
            "QQQ": bars["QQQ"] + flat_bars(start_h=15, start_m=44, n=3, price=500.0)}
    d.feed = FakeFeed(bars=late)
    d.step(ts(15, 46))                                             # close-out + review, before 3:50: no recap
    assert "recap" not in fn.events() and len(brain.review_prompts) == 1
    d.step(ts(15, 50))
    d.step(ts(15, 51))
    recaps = [x for x in fn.sent if x[0] == "recap"]
    assert len(recaps) == 1
    body = recaps[0][2]
    assert "1 trades" in body and "Read 1: 1/60" in body and "account $" in body
    assert "Review: Chased one breakout that failed." in body and "More detail" not in body


def test_recap_follows_a_late_review_in_the_same_pass(tmp_path):
    bars = breakout_set()
    fn = FakeNotifier()
    d, brain = desk_with(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "b"}], fn)
    d.step(bars["AMD"][-1]["t"] + 65)
    late = {"AMD": bars["AMD"] + flat_bars(start_h=15, start_m=54, n=2),
            "QQQ": bars["QQQ"] + flat_bars(start_h=15, start_m=54, n=2, price=500.0)}
    d.feed = FakeFeed(bars=late)
    d.step(ts(15, 56))                                             # desk was late: review then recap, one pass
    assert len(brain.review_prompts) == 1 and "Review: test day" in [x for x in fn.sent if x[0] == "recap"][0][2]


def test_research_done_sends_a_silent_summary(tmp_path):
    fn = FakeNotifier()
    d, _ = desk_with(tmp_path, {}, [], fn, research=True)
    d._sync_research = True
    d.step(ts(8, 46))
    ev = [x for x in fn.sent if x[0] == "research"]
    assert len(ev) == 1 and "2 picks" in ev[0][1] and "High: PLTR" in ev[0][2] and "ENPH" in ev[0][2]


def test_desk_problem_only_after_five_minutes(tmp_path):
    fn = FakeNotifier()
    d, _ = desk_with(tmp_path, {}, [], fn)
    h = d.health
    t0 = ts(11, 0)
    assert h.last_loop_t is None                                   # verdict STARTING
    assert h.watch_problem(t0) is False
    assert h.watch_problem(t0 + 299) is False and fn.sent == []
    assert h.watch_problem(t0 + 301) is True and fn.events() == ["desk_problem"]
    assert h.watch_problem(t0 + 900) is False and len(fn.sent) == 1   # once per episode
    h.loop_done(t0 + 905)                                          # WORKING again: episode over
    assert h.watch_problem(t0 + 906) is False and h.problem_since is None
    d.status.update(feed_ok=False, market="closed")                 # stale feed error after hours: never rings
    for k in (2000, 2400):
        h.loop_done(t0 + k - 1)
        assert h.watch_problem(t0 + k) is False
    d.status.update(market="open")                                  # feed failing while open: does
    h.watch_problem(t0 + 2401)
    h.loop_done(t0 + 2401 + 299)
    assert h.watch_problem(t0 + 2402 + 300) is True and "FEED FAILING" in fn.sent[-1][2]


def test_claude_not_logged_in_raises_banner_rings_once_and_clears(tmp_path):
    """10-31: if --bare becomes the -p default (it ignores the subscription login), every call fails 'Not logged in'.
    The first such failure must put up the banner; the heartbeat thread rings the phone once; a good call clears it."""
    fn = FakeNotifier()
    bars = breakout_set()
    d, brain = desk_with(tmp_path, bars, [], fn)

    def boom(prompt):
        raise RuntimeError("Not logged in · Please run /login")
    brain.decide = boom
    t = bars["AMD"][-1]["t"] + 65
    d.step(t)
    assert d.recent[-1]["action"] == "error"
    assert any(a.startswith("CLAUDE CAN'T BE REACHED") and "not logged in" in a.lower() for a in d.alerts)
    h = d.health
    h.loop_done(t)
    assert "ATTENTION" in h.heartbeat(t)["verdict"]
    assert h.watch_problem(t) is False                              # waits desk_problem_minutes
    h.loop_done(t + 301)
    assert h.watch_problem(t + 301) is True
    assert fn.sent[-1][1] == "Day Desk needs attention" and "sign in" in fn.sent[-1][2]
    h.loop_done(t + 900)
    assert h.watch_problem(t + 900) is False                        # once per episode
    d.note_claude_result(True)                                      # a later call works: banner gone
    assert d.alerts == [] and "ATTENTION" not in h.heartbeat(t + 901)["verdict"]


def test_three_failures_in_a_row_of_any_kind_raise_the_banner(tmp_path):
    d, _ = desk_with(tmp_path, {}, [], FakeNotifier())
    d.note_claude_result(False, "timed out")
    d.note_claude_result(False, "no JSON in the answer")
    assert d.alerts == []
    d.note_claude_result(True)                                      # a success resets the count
    for _ in range(3):
        d.note_claude_result(False, "timed out")
    assert len(d.alerts) == 1 and "3 Claude calls failed in a row" in d.alerts[0]
    d.note_claude_result(False, "timed out")
    assert len(d.alerts) == 1                                       # not stacked


def test_a_broken_notifier_never_breaks_trading(tmp_path):
    bars = breakout_set()
    d, _ = desk_with(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "b"}],
                     FakeNotifier(boom=True))
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "AMD" in d.book.s["positions"] and d.recent[-1]["result"].startswith("bought 10")
    d.health.tick(ts(11, 0))                                       # heartbeat survives too


# ---------------- the page endpoint ----------------
def _post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def test_api_alert_test_and_state(tmp_path):
    post = Recorder()
    n, _ = make_notifier(tmp_path, post)
    d, _ = desk_with(tmp_path, {}, [], n)
    srv = day_desk.ThreadingHTTPServer(("127.0.0.1", 0), day_desk.make_handler(d, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        r = _post(port, "/api/alert-test")
        assert r["ok"] is True and len(post.calls) == 1 and post.calls[0]["headers"]["Priority"] == "3"
        assert _post(port, "/api/alert-test")["ok"] is False         # a second tap right away is held back
        st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())["phone_alerts"]
        assert st["enabled"] and st["topic"] == n.topic and st["subscribe_url"].startswith("ntfy://ntfy.sh/daydesk-")
        d.notifier = None
        assert _post(port, "/api/alert-test")["ok"] is False
    finally:
        srv.shutdown()
