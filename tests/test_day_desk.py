"""Tests for Day Desk. Run TEST.bat (or: .venv\\Scripts\\python -m pytest -q tests)."""
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Book, ET, COST_PER_SIDE, et_now                 # noqa: E402
from scanner import scan, to_5m, Cooldowns                       # noqa: E402
from brain import FakeBrain, parse_decision, build_prompt        # noqa: E402
from feed import FakeFeed                                        # noqa: E402
import day_desk                                                  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = day_desk.load_config(HERE)
WL = ["AMD", "NVDA"]


def ts(h, m, day=28):
    return datetime(2026, 10, day, h, m, tzinfo=ET).timestamp()


def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat_bars(start_h=9, start_m=30, n=60, price=100.0, v=1000, day=28):
    out = []
    t0 = ts(start_h, start_m, day)
    for i in range(n):
        out.append(bar(t0 + 60 * i, price, price + 0.05, price - 0.05, price, v))
    return out


@pytest.fixture
def book(tmp_path):
    return Book(str(tmp_path), CFG)


# ---------------- engine ----------------
def test_buy_applies_cost_and_cash(book):
    assert book.buy("AMD", 100.0, 10, 98.0, 104.0, "test", ts(11, 0), WL) is None
    p = book.s["positions"]["AMD"]
    assert p["entry"] == pytest.approx(100 * (1 + COST_PER_SIDE))
    assert book.s["cash"] == pytest.approx(10000 - 10 * 100 * (1 + COST_PER_SIDE))


@pytest.mark.parametrize("kwargs,why", [
    (dict(sym="TSLA"), "not on the watchlist"),
    (dict(now=ts(15, 30)), "too late"),
    (dict(now=ts(9, 20)), "not open"),
    (dict(shares=30), "above $2,500"),
    (dict(stop=101.0), "stop must be below"),
    (dict(target=99.0), "stop must be below"),
    (dict(stop=90.0), "more than 3.0% away"),
])
def test_buy_limits(book, kwargs, why):
    a = dict(sym="AMD", price=100.0, shares=10, stop=98.0, target=104.0, now=ts(11, 0))
    a.update(kwargs)
    err = book.buy(a["sym"], a["price"], a["shares"], a["stop"], a["target"], "x", a["now"], WL)
    assert err and why in err


def test_max_positions_and_no_double(book):
    wl = ["A", "B", "C", "D", "E"]
    for s in "ABCD":
        assert book.buy(s, 100.0, 5, 98, 104, "x", ts(11, 0), wl) is None
    assert "4 positions" in book.buy("E", 100.0, 5, 98, 104, "x", ts(11, 0), wl)
    assert "already holding" in book.buy("A", 100.0, 5, 98, 104, "x", ts(11, 0), wl)


def test_daily_loss_limit_blocks_new_buys(book):
    start = book.s["day_start_equity"]
    assert not book.day_loss_hit()
    book.s["cash"] = start * (1 - CFG["max_daily_loss_pct"] / 100) + 1        # just short of the limit
    assert not book.day_loss_hit()
    book.s["cash"] = start * (1 - CFG["max_daily_loss_pct"] / 100) - 1        # past it
    assert book.day_loss_hit()
    assert "daily loss limit" in book.buy("AMD", 100.0, 5, 98, 104, "x", ts(11, 0), WL)


def test_daily_loss_counts_open_positions_at_last_price(book):
    assert book.buy("AMD", 100.0, 24, 97.5, 104, "x", ts(11, 0), WL) is None
    book.s["positions"]["AMD"]["last"] = 100.0
    assert not book.day_loss_hit()
    assert book.day_loss_hit({"AMD": 100.0 - (CFG["max_daily_loss_pct"] / 100 * book.s["day_start_equity"] + 5) / 24})


def test_paused_refuses_buys(book):
    book.s["paused"] = True
    assert "paused" in book.buy("AMD", 100.0, 5, 98, 104, "x", ts(11, 0), WL)


def test_stop_fills_at_stop_or_gap_open(book):
    book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(11, 0), WL)
    tr = book.check_exits("AMD", [bar(ts(11, 1), 99.5, 99.8, 99.2, 99.5), bar(ts(11, 2), 99.0, 99.1, 97.5, 97.8)])
    assert tr and tr["exit"] == pytest.approx(98.0 * (1 - COST_PER_SIDE)) and "stop hit 11:02 AM" in tr["why_out"]
    book.buy("NVDA", 100.0, 10, 98.0, 104.0, "x", ts(11, 5), WL)
    tr = book.check_exits("NVDA", [bar(ts(11, 6), 96.0, 96.5, 95.0, 96.2)])   # gapped through: fills at the open
    assert tr["exit"] == pytest.approx(96.0 * (1 - COST_PER_SIDE))


def test_target_and_both_in_one_bar_is_stop(book):
    book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(11, 0), WL)
    tr = book.check_exits("AMD", [bar(ts(11, 1), 103.0, 104.5, 102.9, 104.2)])
    assert tr["exit"] == pytest.approx(104.0 * (1 - COST_PER_SIDE)) and "target" in tr["why_out"]
    book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(11, 10), WL)
    tr = book.check_exits("AMD", [bar(ts(11, 11), 100.0, 105.0, 97.0, 101.0)])
    assert "stop" in tr["why_out"]


def test_old_bars_are_ignored(book):
    book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(11, 0), WL)
    assert book.check_exits("AMD", [bar(ts(10, 59), 99, 99, 90, 95)]) is None   # before the buy
    assert "AMD" in book.s["positions"]


def test_adjust_stop_only_up(book):
    book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(11, 0), WL)
    assert "only move up" in book.adjust("AMD", 97.0, None)
    assert book.adjust("AMD", 99.0, 106.0) is None
    assert book.s["positions"]["AMD"]["stop"] == 99.0


def test_trades_persist_and_reload(tmp_path):
    b = Book(str(tmp_path), CFG)
    b.buy("AMD", 100.0, 10, 98.0, 104.0, "in", ts(11, 0), WL)
    b.sell("AMD", 101.0, ts(11, 30), "out")
    b2 = Book(str(tmp_path), CFG)
    t = b2.trades()
    assert len(t) == 1 and t[0]["sym"] == "AMD" and t[0]["pnl"] > 0
    assert b2.s["cash"] == pytest.approx(b.s["cash"])


def test_new_day_sells_leftovers(book):
    book.s["day"] = "2026-10-27"
    book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(11, 0), WL)
    closed = book.new_day("2026-10-28", {"AMD": 101.0})
    assert len(closed) == 1 and not book.s["positions"] and book.s["calls_today"] == 0


# ---------------- scanner ----------------
def test_breakout_trigger():
    bars = flat_bars(n=40)
    t = bars[-1]["t"] + 60
    bars.append(bar(t, 100.0, 100.6, 100.0, 100.5, v=8000))
    kinds = [k for k, _ in scan("AMD", bars, flat_bars(n=41), CFG)]
    assert "breakout" in kinds


def test_no_trigger_before_start_or_when_quiet():
    assert scan("AMD", flat_bars(n=10), [], CFG) == []
    assert scan("AMD", flat_bars(n=60), flat_bars(n=60), CFG) == []


def test_old_flush_does_not_trigger():
    """10-28: a morning flush kept waking Claude all afternoon. The low must be recent."""
    bars = flat_bars(n=20, price=100.0)
    t = bars[-1]["t"]
    for i in range(10):
        p = 100 - 0.2 * (i + 1)
        bars.append(bar(t + 60 * (i + 1), p + 0.1, p + 0.1, p - 0.05, p))
    t = bars[-1]["t"]
    for i in range(60):                          # an hour of drifting back up: the low is now old
        p = min(98.0 + 0.02 * (i + 1), 98.9)
        bars.append(bar(t + 60 * (i + 1), p - 0.01, p + 0.02, p - 0.01, p))
    assert "flush_bounce" not in [k for k, _ in scan("AMD", bars, flat_bars(n=len(bars)), CFG)]


def test_cooldowns_survive_a_restart(tmp_path):
    p = str(tmp_path / "cool.json")
    c = Cooldowns(CFG, p)
    c.mark("TSLA", "flush_bounce", 5000)
    c2 = Cooldowns(CFG, p)
    assert not c2.ready("TSLA", "flush_bounce", 5000 + 60)


def test_flush_bounce_trigger():
    bars = flat_bars(n=20, price=100.0)
    t = bars[-1]["t"]
    for i in range(10):                         # slide to 98
        p = 100 - 0.2 * (i + 1)
        bars.append(bar(t + 60 * (i + 1), p + 0.1, p + 0.1, p - 0.05, p))
    t = bars[-1]["t"]
    for i in range(5):                          # bounce to 98.9
        p = 98.0 + 0.18 * (i + 1)
        bars.append(bar(t + 60 * (i + 1), p - 0.1, p + 0.05, p - 0.1, p))
    assert "flush_bounce" in [k for k, _ in scan("AMD", bars, flat_bars(n=len(bars)), CFG)]


def test_to_5m_groups():
    b = to_5m(flat_bars(n=12))
    assert len(b) == 3 and b[0]["v"] == 5000


def test_cooldowns():
    c = Cooldowns(CFG)
    assert c.ready("AMD", "breakout", 1000)
    c.mark("AMD", "breakout", 1000)
    assert not c.ready("AMD", "momentum", 1000 + 60)
    assert c.ready("AMD", "momentum", 1000 + 11 * 60)
    assert not c.ready("AMD", "breakout", 1000 + 11 * 60)


# ---------------- brain ----------------
def test_parse_decision_tolerates_text_around_json():
    d = parse_decision('Sure:\n{"action":"BUY","shares":"4","stop":"98.5","target":104,"why":"x"}\nthanks')
    assert d["action"] == "buy" and d["shares"] == 4 and d["stop"] == 98.5


def test_parse_decision_rejects_garbage():
    with pytest.raises(Exception):
        parse_decision("no idea")
    with pytest.raises(Exception):
        parse_decision('{"action":"short"}')


def test_prompt_mentions_key_facts():
    bars = flat_bars(n=40)
    p = build_prompt("AMD", "new high", bars, flat_bars(n=40), ["10-28 13:00 ET | AMD up"],
                     {"cash": 10000, "positions": {}}, 24)
    assert "AMD" in p and "max shares you may buy now: 24" in p and "Last 30 one-minute bars" in p


# ---------------- the desk loop ----------------
def make_desk(tmp_path, bars, answers, research=False):
    feed = FakeFeed(bars=bars)
    brain = FakeBrain(answers)
    cfg = CFG if research else dict(CFG, research_minute=10 ** 6)     # keep research out of unrelated tests
    d = day_desk.Desk(str(tmp_path), cfg, feed, brain)
    d.set_watchlist(["AMD"])
    return d, brain


def breakout_set(n=40):
    amd = flat_bars(n=n)
    amd.append(bar(amd[-1]["t"] + 60, 100.0, 100.6, 100.0, 100.5, v=8000))
    return {"AMD": amd, "QQQ": flat_bars(n=n + 1, price=500.0)}


def test_desk_buys_on_trigger_and_claude_yes(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "breakout holding"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "AMD" in d.book.s["positions"] and len(brain.prompts) == 1
    assert d.recent[-1]["result"].startswith("bought 10")
    assert d.book.s["calls_today"] == 1


def test_desk_refuses_bad_claude_answer(tmp_path):
    bars = breakout_set()
    d, _ = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 90.0, "target": 103.0, "why": "too wide"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert not d.book.s["positions"] and d.recent[-1]["result"].startswith("refused")


def test_desk_caps_shares_at_budget(tmp_path):
    bars = breakout_set()
    d, _ = make_desk(tmp_path, bars, [{"action": "buy", "shares": 1000, "stop": 99.0, "target": 103.0, "why": "x"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert d.book.s["positions"]["AMD"]["shares"] == 24      # floor(2500 / 100.51)


def test_desk_respects_call_cap(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [])
    d.book.s["calls_today"] = CFG["max_calls_per_day"]
    d.book.s["day"] = "2026-10-28"
    d.step(bars["AMD"][-1]["t"] + 65)
    assert not brain.prompts and "daily cap" in d.recent[-1]["result"]


def test_desk_flattens_at_345(tmp_path):
    amd = flat_bars(start_h=15, start_m=40, n=6)
    bars = {"AMD": amd, "QQQ": flat_bars(start_h=15, start_m=40, n=6, price=500.0)}
    d, _ = make_desk(tmp_path, bars, [])
    d.book.s["day"] = "2026-10-28"
    d.book.buy("AMD", 100.0, 10, 98.0, 104.0, "x", ts(15, 0), ["AMD"])
    d.step(ts(15, 46))
    assert not d.book.s["positions"] and d.book.trades()[-1]["why_out"] == "end-of-day flat"


def test_desk_without_key_only_watches(tmp_path):
    bars = breakout_set()
    d = day_desk.Desk(str(tmp_path), CFG, FakeFeed(bars=bars), None)
    d.set_watchlist(["AMD"])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert not d.book.s["positions"] and "Claude Code" in d.recent[-1]["result"]


def test_desk_closed_on_weekend(tmp_path):
    d, brain = make_desk(tmp_path, {}, [])
    d.step(datetime(2026, 10, 31, 11, 0, tzinfo=ET).timestamp())
    assert d.status["market"] == "closed"


def test_watchlist_validation(tmp_path):
    d, _ = make_desk(tmp_path, {}, [])
    assert d.set_watchlist(["msft", "BRK.B"]) is None and d.watchlist() == ["MSFT", "BRK.B"]
    assert "doesn't look like" in d.set_watchlist(["BTC-USD"])
    assert "holds" in d.set_watchlist([f"A{chr(65 + i)}" for i in range(16)])


# ---------------- web server ----------------
def test_server_requires_key_and_serves_state(tmp_path):
    import shutil
    shutil.copy(os.path.join(HERE, "page.html"), tmp_path / "page.html")
    d, _ = make_desk(tmp_path, {}, [])
    srv = day_desk.ThreadingHTTPServer(("127.0.0.1", 0), day_desk.make_handler(d, "abc123"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"Day Desk" in urllib.request.urlopen(base + "/").read()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/state")
        assert e.value.code == 401
        req = urllib.request.Request(base + "/api/state", headers={"X-Key": "abc123"})
        st = json.loads(urllib.request.urlopen(req).read())
        assert st["watchlist"] == ["AMD"] and st["equity"] == 10000
        req = urllib.request.Request(base + "/api/watchlist", data=json.dumps({"symbols": ["AMD", "MSFT"]}).encode(),
                                     headers={"X-Key": "abc123", "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req)
        assert d.watchlist() == ["AMD", "MSFT"]
    finally:
        srv.shutdown()


def test_cli_brain_strips_api_key_and_parses(monkeypatch, tmp_path):
    """The Claude Code call must never see an API key (that would bill the API account)."""
    import subprocess
    import brain as B
    seen = {}

    class R:
        returncode = 0
        stdout = '{"action":"pass","why":"chop"}'
        stderr = ""

    def fake_run(args, **kw):
        seen["args"], seen["env"], seen["input"] = args, kw["env"], kw["input"]
        assert "creationflags" in kw
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    d, _ = B.ClaudeCLIBrain("claude", "sonnet").decide("hello")
    assert d["action"] == "pass"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["env"]["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"
    assert seen["args"][:2] == ["claude", "-p"] and "hello" in seen["input"]


def test_cli_brain_runs_outside_every_project_folder(monkeypatch):
    """claude -p reads every CLAUDE.md from its start folder upward. Every call (decide, review, research, ping) must
    start in the empty temp folder: never in this program's folder, never in any folder above it (10-31: Day Desk
    moved inside daytrading-bot, whose old-bot CLAUDE.md must not reach trading calls)."""
    import subprocess
    import brain as B
    from pathlib import Path
    cwds = []

    class R:
        returncode = 0
        stdout = '{"action":"pass","why":"x","ok":true,"summary":"s","grades":[],"lessons":[],"picks":[],"backdrop":""}'
        stderr = ""

    def fake_run(args, **kw):
        cwds.append(kw.get("cwd"))
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    b = B.ClaudeCLIBrain("claude", "sonnet")
    b.decide("x")
    b.ping()
    for call in (lambda: b.review("x"), lambda: b.research("x")):
        try:
            call()
        except Exception:
            pass                          # parse errors on the fake answer don't matter; the cwd was recorded
    assert len(cwds) == 4 and all(cwds), "every call must pass an explicit cwd"
    prog = Path(HERE).resolve()
    for c in cwds:
        c = Path(c).resolve()
        assert c.is_dir() and c.name == B.CLAUDE_WORKDIR_NAME
        assert not c.is_relative_to(prog), "a call started inside the program folder"
        assert not prog.is_relative_to(c), "a call started in a folder above the program (would read its CLAUDE.md)"


def test_cli_brain_reports_failure(monkeypatch):
    import subprocess
    import brain as B

    class R:
        returncode = 1
        stdout = ""
        stderr = "not logged in"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="not logged in"):
        B.ClaudeCLIBrain("claude", "sonnet").decide("x")


def test_server_without_key_is_open(tmp_path):
    import shutil
    shutil.copy(os.path.join(HERE, "page.html"), tmp_path / "page.html")
    d, _ = make_desk(tmp_path, {}, [])
    srv = day_desk.ThreadingHTTPServer(("127.0.0.1", 0), day_desk.make_handler(d, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/state").read())
        assert st["watchlist"] == ["AMD"]
    finally:
        srv.shutdown()


# ---------------- learning from its own trading ----------------
def test_review_runs_once_after_close_and_feeds_the_next_prompt(tmp_path):
    bars = breakout_set()
    rv = {"summary": "Chased one breakout that failed.", "lessons": [
        {"rule": "Wait for a 5-minute close above the breakout level before buying", "evidence": "AMD 10-28", "status": "tentative"}]}
    d, brain = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "breakout"}])
    brain.review_answer = rv
    d.step(bars["AMD"][-1]["t"] + 65)                      # buys AMD
    assert "AMD" in d.book.s["positions"]
    late = {"AMD": bars["AMD"] + flat_bars(start_h=15, start_m=44, n=3), "QQQ": bars["QQQ"] + flat_bars(start_h=15, start_m=44, n=3, price=500.0)}
    d.feed = FakeFeed(bars=late)
    d.step(ts(15, 46))                                     # flatten + review
    d.step(ts(15, 47))                                     # must NOT review twice
    assert len(brain.review_prompts) == 1
    assert "AMD" in brain.review_prompts[0] and "Trades closed today (1)" in brain.review_prompts[0]
    assert d.lessons()["lessons"][0]["rule"].startswith("Wait for a 5-minute close")
    assert d.snapshot()["learned"]["summary"] == "Chased one breakout that failed."
    # next day's decision prompt carries the playbook
    nxt = {"AMD": [dict(b, t=b["t"] + 86400) for b in bars["AMD"]], "QQQ": [dict(b, t=b["t"] + 86400) for b in bars["QQQ"]]}
    d.feed = FakeFeed(bars=nxt)
    brain.answers = [{"action": "pass", "why": "following my rule"}]
    d.step(nxt["AMD"][-1]["t"] + 65)
    assert "Wait for a 5-minute close above the breakout level" in brain.prompts[-1]


def test_review_shows_what_happened_after_a_pass():
    from brain import build_review_prompt
    amd = flat_bars(n=40)
    t0 = amd[-1]["t"]
    amd += [bar(t0 + 60 * i, 100.0, 100.0 + 0.1 * i, 99.9, 100.0 + 0.1 * i) for i in range(1, 21)]
    dec = [{"t": t0, "sym": "AMD", "trigger": "breakout", "reason": "x", "action": "pass", "why": "chop", "result": "no trade", "price": 100.0}]
    p = build_review_prompt("2026-10-28", [], dec, {"AMD": amd}, [])
    assert "you said PASS" in p and "best +2.00%" in p


def test_review_skipped_on_a_day_with_nothing_to_learn(tmp_path):
    d, brain = make_desk(tmp_path, {"AMD": flat_bars(start_h=15, start_m=44, n=3), "QQQ": flat_bars(start_h=15, start_m=44, n=3, price=500.0)}, [])
    d.step(ts(15, 46))
    assert brain.review_prompts == []


def test_parse_review_caps_and_cleans():
    from brain import parse_review
    d = parse_review('ok {"summary":"s","lessons":[' + ",".join('{"rule":"r%d","status":"SOLID"}' % i for i in range(20)) + ',{"rule":""}]}')
    assert len(d["lessons"]) == 15 and d["lessons"][0]["status"] == "solid"


# ---------------- strategies: tagged, scored, graded ----------------
def test_buy_is_tagged_and_scored(tmp_path):
    from engine import scorecard
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "buy", "setup": "Breakout Hold!", "shares": 10, "stop": 99.0, "target": 103.0, "why": "x"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert d.book.s["positions"]["AMD"]["setup"] == "breakout-hold"
    d.book.sell("AMD", 101.0, bars["AMD"][-1]["t"] + 600, "out")
    sc = scorecard(d.book.trades())
    assert sc[0]["setup"] == "breakout-hold" and sc[0]["trades"] == 1 and sc[0]["pnl"] > 0
    assert d.snapshot()["scorecard"][0]["setup"] == "breakout-hold"


def test_old_trade_file_gets_the_new_column(tmp_path):
    os.makedirs(tmp_path / "data")
    (tmp_path / "data" / "trades.csv").write_text(
        "sym,shares,entry,exit,entry_t,exit_t,pnl,pnl_pct,why_in,why_out\nAMD,1,100,101,1,2,1,1,a,b\n")
    b = Book(str(tmp_path), CFG)
    b.buy("AMD", 100.0, 1, 98, 104, "x", ts(11, 0), WL, "vwap-reclaim")
    b.sell("AMD", 101.0, ts(11, 5), "y")
    t = b.trades()
    assert len(t) == 2 and t[0]["setup"] == "" and t[1]["setup"] == "vwap-reclaim"


def test_review_grades_strategies_and_prompt_shows_scorecard(tmp_path):
    bars = breakout_set()
    rv = {"summary": "s", "lessons": [], "strategies": [{"setup": "breakout-hold", "verdict": "needs more data", "note": "one trade"}]}
    d, brain = make_desk(tmp_path, bars, [{"action": "buy", "setup": "breakout-hold", "shares": 10, "stop": 99.0, "target": 103.0, "why": "x"}])
    brain.review_answer = rv
    d.step(bars["AMD"][-1]["t"] + 65)
    late = {"AMD": bars["AMD"] + flat_bars(start_h=15, start_m=44, n=3), "QQQ": bars["QQQ"] + flat_bars(start_h=15, start_m=44, n=3, price=500.0)}
    d.feed = FakeFeed(bars=late)
    d.step(ts(15, 46))
    assert "breakout-hold: 1 trades" in brain.review_prompts[0]
    assert d.lessons()["strategies"][0]["verdict"] == "needs more data"
    nxt = {"AMD": [dict(b, t=b["t"] + 86400) for b in bars["AMD"]], "QQQ": [dict(b, t=b["t"] + 86400) for b in bars["QQQ"]]}
    d.feed = FakeFeed(bars=nxt)
    brain.answers = [{"action": "pass", "why": "x"}]
    d.step(nxt["AMD"][-1]["t"] + 65)
    assert "Your scorecard by strategy" in brain.prompts[-1] and "breakout-hold: 1 trades" in brain.prompts[-1]


# ---------------- morning research ----------------
def test_research_runs_once_each_trading_morning_and_feeds_decisions(tmp_path):
    d, brain = make_desk(tmp_path, {}, [], research=True)
    brain.research_prompts = []
    d._sync_research = True
    d.step(ts(8, 30))                               # too early
    assert brain.research_prompts == []
    d.step(ts(8, 46)); d.step(ts(8, 50))           # runs once
    assert len(brain.research_prompts) == 1
    r = d.todays_research(ts(9, 0))
    assert r["picks"][0]["ticker"] == "PLTR"
    assert d.watchlist() == ["AMD", "PLTR"]        # high conviction auto-added; ENPH (medium) is only suggested
    d.step(datetime(2026, 10, 31, 8, 50, tzinfo=ET).timestamp())               # Saturday: nothing
    assert len(brain.research_prompts) == 1
    # a decision on a researched stock carries the research
    from brain import fmt_research_for
    assert "beat earnings" in fmt_research_for("PLTR", r) and "Futures flat." in fmt_research_for("AMD", r)


def test_research_session_and_weekend_preview(tmp_path):
    """10-31: a weekend/evening 'Research now' previews the NEXT session (searches, saved under that date,
    never auto-adds); a weekday before 4 PM is today. Saturday's lean run answered 'market closed' with no search."""
    d, brain = make_desk(tmp_path, {}, [], research=True)
    sat = datetime(2026, 10, 31, 11, 0, tzinfo=ET).timestamp()
    assert d.research_session(ts(8, 46)) == ("2026-10-28", False)
    assert d.research_session(ts(16, 30)) == ("2026-10-29", True)                  # Wed evening -> Thu
    assert d.research_session(sat) == ("2026-11-02", True)                         # Sat -> Mon
    assert d.research_session(datetime(2026, 11, 26, 18, 0, tzinfo=ET).timestamp()) == ("2026-11-30", True)  # skips 11-27
    brain.research_prompts = []
    assert d.run_research(sat, sync=True) == "started"
    assert "PREVIEW" in brain.research_prompts[0] and "Monday November 02, 2026" in brain.research_prompts[0]
    r = d.research()
    assert r["day"] == "2026-11-02" and r["preview"] is True
    assert d.watchlist() == ["AMD"]                                                 # PLTR is high, but no auto-add
    assert d.recent[-1]["result"].startswith("PREVIEW for 2026-11-02")
    # Monday 8:45: the real run replaces the preview and auto-adds as usual
    d._sync_research = True
    d.step(datetime(2026, 11, 2, 8, 46, tzinfo=ET).timestamp())
    assert d.research()["preview"] is False and "PLTR" in d.watchlist()


def test_research_warns_when_no_web_search_was_made(tmp_path):
    d, brain = make_desk(tmp_path, {}, [], research=True)
    brain.last_usage = lambda: {"tok_in": 3000, "searches": 0, "turns": 1}
    d.run_research(ts(8, 46), sync=True)
    assert "WARNING: no web search" in d.recent[-1]["result"]
    brain.last_usage = lambda: {"tok_in": 90000, "searches": 7, "turns": 9}
    d.run_research(ts(8, 50), sync=True)
    assert "WARNING" not in d.recent[-1]["result"]


def test_research_auto_add_modes(tmp_path):
    d, brain = make_desk(tmp_path, {}, [], research=True)
    d.cfg = dict(CFG, auto_add_picks=False)
    d._sync_research = True
    d.step(ts(8, 46))
    assert d.watchlist() == ["AMD"]


def test_auto_added_come_off_next_morning_unless_held_or_repicked(tmp_path):
    d, brain = make_desk(tmp_path, {}, [])
    picks_day1 = [{"ticker": "PLTR", "conviction": "high"}, {"ticker": "VRT", "conviction": "high"}]
    assert d.apply_auto_picks("2026-10-28", picks_day1) == ["PLTR", "VRT"]
    d.book.s["positions"]["VRT"] = {"shares": 1, "entry": 1, "entry_t": 0, "stop": 0.5, "target": 2, "why": "x"}
    d.set_watchlist(d.watchlist() + ["MSFT"])          # tomahawkeer adds one himself
    d.apply_auto_picks("2026-10-29", [{"ticker": "NVDA", "conviction": "medium"}])
    wl = d.watchlist()
    assert "PLTR" not in wl and "VRT" in wl and "MSFT" in wl and "AMD" in wl and "NVDA" not in wl
    d.book.s["positions"].pop("VRT")
    d.apply_auto_picks("2026-10-30", [{"ticker": "VRT", "conviction": "high"}])
    assert "VRT" in d.watchlist() and d.auto_added()["VRT"] == "2026-10-30"


def test_parse_research_cleans_tickers():
    from brain import parse_research
    r = parse_research('x {"backdrop":"b","picks":[{"ticker":"$nvda","conviction":"HIGH"},{"ticker":"NVDA"},{"ticker":"BTC-USD"},{"ticker":"BRK.B","conviction":"maybe"}]}')
    assert [p["ticker"] for p in r["picks"]] == ["NVDA", "BRK.B"]
    assert [p["conviction"] for p in r["picks"]] == ["high", "medium"]


def test_cli_research_allows_only_web_tools(monkeypatch):
    import subprocess
    import brain as B
    seen = {}

    class R:
        returncode = 0
        stdout = '{"backdrop":"b","picks":[]}'
        stderr = ""

    def fake_run(args, **kw):
        seen["args"] = args
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    B.ClaudeCLIBrain("claude", "opus").research("go")
    a = seen["args"]
    assert a[a.index("--tools") + 1] == "WebSearch,WebFetch" and "--allowedTools" in a
    B.ClaudeCLIBrain("claude", "opus").decide.__func__  # decisions: no tools
    R.stdout = '{"action":"pass","why":"x"}'
    B.ClaudeCLIBrain("claude", "opus").decide("x")
    a = seen["args"]
    assert a[a.index("--tools") + 1] == "" and "--allowedTools" not in a


# ---------------- proof of life: heartbeat + mailbox (10-29) ----------------
def _ask(d, name, text):
    with open(os.path.join(d.health.inbox, name), "w", encoding="utf-8") as f:
        f.write(text)
    d.health.check_inbox()


def _answer(d, name):
    with open(d.health._answer_path(name), encoding="utf-8") as f:
        return json.load(f)


def test_heartbeat_file_and_verdicts(tmp_path):
    d, _ = make_desk(tmp_path, {}, [])
    h = d.health
    h.write_heartbeat(ts(8, 0))
    hb = json.load(open(h.hb_path, encoding="utf-8"))
    assert hb["alive"] and hb["verdict"].startswith("STARTING") and hb["pid"] == os.getpid()
    h.loop_done(ts(8, 0))
    assert json.load(open(h.hb_path, encoding="utf-8"))["verdict"].startswith("WORKING")   # first pass rewrites it
    assert h.heartbeat(ts(8, 1))["verdict"].startswith("WORKING")
    assert h.heartbeat(ts(8, 0) + CFG["loop_stuck_seconds"] + 60)["verdict"].startswith("LOOP STUCK")
    d.status.update(feed_ok=False, error="price feed failed: boom")
    assert "FEED FAILING" in h.heartbeat(ts(8, 1))["verdict"]


def test_mailbox_ping_help_unknown_and_never_twice(tmp_path):
    d, _ = make_desk(tmp_path, {}, [])
    _ask(d, "ping-0742.txt", "ping\n")
    a = _answer(d, "ping-0742.txt")
    assert a["ok"] and a["done"] and a["detail"]["alive"] and a["command"] == "ping"
    _ask(d, "help-1.txt", "HELP")
    assert set(_answer(d, "help-1.txt")["detail"]) == {"ping", "feed", "claude", "help"}
    _ask(d, "x-1.txt", "launch the rockets")
    assert not _answer(d, "x-1.txt")["ok"]
    os.remove(d.health._answer_path("ping-0742.txt"))   # even with the answer gone, a used name is ignored
    assert d.health.check_inbox() == []
    assert not os.path.exists(d.health._answer_path("ping-0742.txt"))


def test_mailbox_empty_file_uses_name(tmp_path):
    d, _ = make_desk(tmp_path, {}, [])
    _ask(d, "ping-empty.txt", "")
    assert _answer(d, "ping-empty.txt")["ok"]


def test_mailbox_feed_runs_in_the_loop(tmp_path):
    bars = breakout_set()
    d, _ = make_desk(tmp_path, bars, [])
    _ask(d, "feed-1.txt", "feed")
    assert _answer(d, "feed-1.txt")["done"] is False            # queued, not fetched by the mailbox thread
    d.health.loop_done(ts(11, 0))
    a = _answer(d, "feed-1.txt")
    assert a["ok"] and a["done"] and a["detail"]["AMD"]["bars_today"] == len(bars["AMD"])

    class Broken:
        def bars_1m(self, syms):
            raise RuntimeError("yahoo down")
    d.feed = Broken()
    _ask(d, "feed-2.txt", "feed")
    d.health.loop_done(ts(11, 1))
    a = _answer(d, "feed-2.txt")
    assert not a["ok"] and "FAILED" in a["summary"]


def _wait_done(d, name):
    import time as _t
    for _ in range(1000):                       # up to 10 s: the Windows write retry alone can take 2 s
        a = _answer(d, name)
        if a["done"]:
            return a
        _t.sleep(0.01)
    raise AssertionError("claude self-test never finished")


def test_mailbox_claude_logged_capped_and_kept_out_of_review(tmp_path):
    d, _ = make_desk(tmp_path, {}, [])
    _ask(d, "claude-1.txt", "claude")
    a = _wait_done(d, "claude-1.txt")
    assert a["ok"] and "DAY DESK OK" in a["detail"]["reply"]
    for _ in range(200):                                      # logged like every Claude call (rule 4)
        if any(r.get("trigger") == "self-test" for r in d.recent):
            break
        threading.Event().wait(0.01)
    assert any(r.get("trigger") == "self-test" for r in d.recent)
    assert d.book.s["calls_today"] == 0                        # not charged to the trading cap
    for i in range(2, CFG["selftest_claude_max_per_day"] + 1):
        _ask(d, f"claude-{i}.txt", "claude")
        _wait_done(d, f"claude-{i}.txt")
    _ask(d, "claude-over.txt", "claude")
    a = _answer(d, "claude-over.txt")
    assert not a["ok"] and "refused" in a["summary"]


def test_mailbox_claude_failure_reported(tmp_path):
    d, brain = make_desk(tmp_path, {}, [])
    brain.ping_reply = RuntimeError("not logged in")
    _ask(d, "claude-f.txt", "claude")
    a = _wait_done(d, "claude-f.txt")
    assert not a["ok"] and "not logged in" in a["summary"]


def test_self_test_never_reaches_the_review(tmp_path):
    d, brain = make_desk(tmp_path, {}, [])
    d.recent.append({"t": ts(12, 0), "sym": "-", "trigger": "self-test", "action": "self-test", "result": "ok"})
    d.end_of_day_review("2026-10-28", {}, ts(15, 50))
    assert brain.review_prompts == []                         # only a self-test today -> nothing to review


def test_api_ping(tmp_path):
    d, _ = make_desk(tmp_path, {}, [])
    srv = day_desk.ThreadingHTTPServer(("127.0.0.1", 0), day_desk.make_handler(d, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        hb = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/ping").read())
        assert hb["alive"] and "verdict" in hb
    finally:
        srv.shutdown()


def test_write_json_survives_windows_file_lock(tmp_path, monkeypatch):
    """Windows: os.replace fails while another reader has the file open (TEST.bat on the desk PC, 10-29)."""
    import health
    real, calls = os.replace, {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(5, "Access is denied")
        return real(a, b)
    monkeypatch.setattr(health.os, "replace", flaky)
    p = str(tmp_path / "x.json")
    day_desk.write_json(p, {"ok": 1})
    assert json.load(open(p, encoding="utf-8")) == {"ok": 1} and calls["n"] == 4

    monkeypatch.setattr(health.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "denied")))
    monkeypatch.setattr(health, "REPLACE_WAIT_S", 0)
    with pytest.raises(PermissionError):                     # a lock that never clears is still reported
        health.write_json(p, {"ok": 2})
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]   # and no temp file left behind


def test_desk_stops_asking_after_daily_loss_limit_but_still_reviews(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "x"}])
    d.book.s["cash"] = d.book.s["day_start_equity"] * (1 - CFG["max_daily_loss_pct"] / 100) - 10
    d.book.s["day"] = "2026-10-28"                                       # same day as the bars: no rollover
    d.step(bars["AMD"][-1]["t"] + 65)
    assert brain.prompts == [] and "loss limit" in d.status["market"]   # no call spent on a refused buy
    # an open position is still reviewed
    d.book.s["positions"]["AMD"] = {"shares": 1, "entry": 100.0, "entry_t": bars["AMD"][0]["t"], "stop": 99.0,
                                    "target": 103.0, "why": "x", "last": 100.0, "setup": "t"}
    d.step(bars["AMD"][-1]["t"] + 6)
    assert len(brain.prompts) == 1 and "review" in d.recent[-1]["trigger"]


# ---------------- watch-only research picks (10-30) ----------------
def _today_picks(d, picks):
    day_desk.write_json(d.picks_path, {"day": "2026-10-28", "t": ts(8, 50), "status": "done", "backdrop": "",
                                       "picks": picks})


def _rising(n, start=50.0, step=0.1, day=28):
    """1-minute bars from 9:30 rising `step` each minute, with a volume spike on the last bar (a breakout)."""
    t0 = ts(9, 30, day)
    out = [bar(t0 + 60 * i, start + step * i, start + step * i + 0.05, start + step * i - 0.05, start + step * i)
           for i in range(n)]
    out[-1]["v"] = 50000
    return out


def test_watch_only_picks_are_priced_but_never_asked_about_or_bought(tmp_path):
    bars = {"AMD": flat_bars(n=60), "QQQ": flat_bars(n=60, price=500.0), "PLTR": _rising(60)}
    d, brain = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 55.0, "target": 60.0, "why": "x"}])
    d.book.s["day"] = "2026-10-28"
    _today_picks(d, [{"ticker": "PLTR", "conviction": "medium", "catalyst": "beat"}])
    assert d.watch_only_picks(ts(10, 0), d.watchlist()) == ["PLTR"]
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "PLTR" in d.pick_moves and d.pick_moves["PLTR"] > 0      # it was priced
    assert brain.prompts == [] and "PLTR" not in d.book.s["positions"]  # and never traded
    assert d.watchlist() == ["AMD"]
    d.cfg = dict(d.cfg, price_all_picks=False)
    assert d.watch_only_picks(ts(10, 0), d.watchlist()) == []


def test_watch_only_pick_cap_and_watchlist_picks_not_doubled(tmp_path):
    d, _ = make_desk(tmp_path, {}, [])
    _today_picks(d, [{"ticker": t} for t in ["AMD", "A", "B", "C"]])
    d.cfg = dict(d.cfg, max_watch_only_picks=2)
    assert d.watch_only_picks(ts(10, 0), d.watchlist()) == ["A", "B"]   # AMD is already on the watchlist


def test_picks_scored_once_at_close_out_against_qqq(tmp_path):
    n = 380                                          # 9:30 .. 15:49
    qqq = _rising(n, start=500.0, step=0.0)          # flat QQQ
    pltr = _rising(n, start=50.0, step=0.01)
    bars = {"AMD": flat_bars(n=n), "QQQ": qqq, "PLTR": pltr}
    d, brain = make_desk(tmp_path, bars, [])
    d.book.s["day"] = "2026-10-28"
    _today_picks(d, [{"ticker": "PLTR", "conviction": "medium"}, {"ticker": "ZZZZ", "conviction": "low"}])
    d.step(ts(15, 45) + 5); d.step(ts(15, 46) + 5)
    rows = [json.loads(l) for l in open(os.path.join(str(tmp_path), "data", "pick_results.jsonl"))]
    assert len(rows) == 2                                               # once, not per tick
    p = rows[0]
    assert p["ticker"] == "PLTR" and p["tradable"] is False and p["qqq_945_345_pct"] == 0.0
    a, z = 50.0 + 0.01 * 15, 50.0 + 0.01 * 375                          # 9:45 bar and 3:45 bar closes
    assert p["px_945"] == pytest.approx(a) and p["px_345"] == pytest.approx(z)
    assert p["move_945_345_pct"] == pytest.approx(round((z / a - 1) * 100, 3))
    assert p["vs_qqq_pct"] == p["move_945_345_pct"]
    assert rows[1]["ticker"] == "ZZZZ" and rows[1]["move_945_345_pct"] is None and rows[1]["bars"] == 0
    assert any(r.get("trigger") == "research grading" for r in d.recent)


def test_bars_archived_once_at_close_out(tmp_path):
    n = 380
    bars = {"AMD": flat_bars(n=n), "QQQ": flat_bars(n=n, price=500.0)}
    d, _ = make_desk(tmp_path, bars, [])
    d.book.s["day"] = "2026-10-28"
    d.step(ts(15, 30) + 5)
    p = os.path.join(str(tmp_path), "data", "bars", "2026-10-28.json")
    assert not os.path.exists(p)                                  # not before the close-out
    d.step(ts(15, 45) + 5)
    saved = json.load(open(p))
    assert saved["day"] == "2026-10-28" and len(saved["bars"]["AMD"]) == n and "QQQ" in saved["bars"]
    m = os.path.getmtime(p)
    d.step(ts(15, 46) + 5)
    assert os.path.getmtime(p) == m                               # once a day


# ---------- usage tracking (10-31) ----------
CLI_JSON = json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 4200,
                       "num_turns": 1, "result": '{"action":"pass","why":"chop"}', "total_cost_usd": 0.0312,
                       "usage": {"input_tokens": 12, "cache_creation_input_tokens": 3000,
                                 "cache_read_input_tokens": 9000, "output_tokens": 150}})


def test_unwrap_cli_json_reads_answer_and_usage():
    import brain as B
    text, u = B.unwrap_cli_json(CLI_JSON)
    assert text == '{"action":"pass","why":"chop"}'
    assert u["tok_in"] == 12012 and u["tok_cached"] == 9000 and u["tok_new"] == 3012 and u["tok_out"] == 150
    assert u["cost_usd"] == 0.0312 and u["secs"] == 4.2


def test_unwrap_cli_json_falls_back_to_plain_text():
    import brain as B
    assert B.unwrap_cli_json('{"action":"pass","why":"x"}') == ('{"action":"pass","why":"x"}', None)
    assert B.unwrap_cli_json("not json at all") == ("not json at all", None)


def test_unwrap_cli_json_raises_on_reported_error():
    import brain as B
    bad = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})
    with pytest.raises(RuntimeError):
        B.unwrap_cli_json(bad)


def test_cli_brain_asks_for_json_and_keeps_usage_per_thread(monkeypatch):
    import subprocess
    import brain as B
    seen = {}

    class R:
        returncode = 0
        stdout = CLI_JSON
        stderr = ""

    def fake_run(args, **kw):
        seen["args"] = args
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    b = B.ClaudeCLIBrain("claude", "sonnet")
    d, _ = b.decide("x")
    assert d["action"] == "pass"
    i = seen["args"].index("--output-format")
    assert seen["args"][i + 1] == "json"
    assert b.last_usage()["cost_usd"] == 0.0312
    other = {}
    t = threading.Thread(target=lambda: other.setdefault("u", b.last_usage()))
    t.start(); t.join()
    assert other["u"] is None                     # another thread never sees this thread's usage


def test_every_claude_call_is_logged_with_its_usage(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "pass", "why": "meh"}])
    brain.last_usage = lambda: {"tok_in": 100, "tok_out": 10, "cost_usd": 0.02}
    d.step(bars["AMD"][-1]["t"] + 65)
    assert d.recent[-1]["usage"]["cost_usd"] == 0.02
    s = d.usage_summary()
    day = s["days"][-1]
    assert day["calls"] == 1 and day["unmeasured"] == 0 and day["cost_usd"] == 0.02
    assert day["by_kind"]["trading"]["calls"] == 1


def test_usage_summary_skips_engine_rows_and_counts_old_rows_as_unmeasured(tmp_path):
    d, _ = make_desk(tmp_path, breakout_set(), [])
    t = ts(11, 0, day=30)
    for rec in ({"t": t, "trigger": "momentum", "action": "pass", "why": "old row, no usage"},
                {"t": t, "trigger": "resting order", "action": "sell", "why": "stop hit"},
                {"t": t, "trigger": "3:45 PM close-out", "action": "sell"},
                {"t": t, "trigger": "momentum", "action": "-", "result": "skipped: daily cap"},
                {"t": t, "trigger": "morning research", "action": "research", "usage": {"cost_usd": 0.5,
                                                                                      "tok_in": 1, "tok_out": 1}}):
        d.record(rec)
    day = d.usage_summary()["days"][-1]
    assert day["calls"] == 2 and day["unmeasured"] == 1 and day["cost_usd"] == 0.5
    assert set(day["by_kind"]) == {"trading", "research"}


# ---------- learning-loop facts (10-31) ----------
def test_review_gets_measured_lab_facts_and_decisions_get_noise_and_earlier_today(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "breakout"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "Typical 1-minute range today:" in brain.prompts[0]
    assert "Earlier today:" not in brain.prompts[0]            # nothing happened in AMD yet
    late = {"AMD": bars["AMD"] + flat_bars(start_h=15, start_m=44, n=3), "QQQ": bars["QQQ"] + flat_bars(start_h=15, start_m=44, n=3, price=500.0)}
    d.feed = FakeFeed(bars=late)
    d.step(ts(15, 46))                                         # close-out archives bars, then the review runs
    assert "MEASURED FACTS from the trade lab" in brain.review_prompts[0]
    assert "Hindsight on 1 trades" in brain.review_prompts[0]
    # a later ask the same day carries what already happened in the stock
    note = d.earlier_today("AMD", ts(15, 50), 100.0)
    assert note.startswith("Earlier today:") and "AMD" in note


def test_lab_failure_never_blocks_a_trading_call(tmp_path, monkeypatch):
    import lab
    monkeypatch.setattr(lab, "symbol_today", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "pass", "why": "meh"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert len(brain.prompts) == 1 and d.recent[-1]["action"] == "pass"


def test_close_out_refreshes_the_lab_report_once_for_the_page(tmp_path):
    bars = breakout_set()
    d, _ = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "breakout"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    late = {"AMD": bars["AMD"] + flat_bars(start_h=15, start_m=44, n=3), "QQQ": bars["QQQ"] + flat_bars(start_h=15, start_m=44, n=3, price=500.0)}
    d.feed = FakeFeed(bars=late)
    d.step(ts(15, 46))
    lab = d.snapshot()["lab"]
    assert lab and lab["n_trades"] == 1 and "caveat" in lab["stop_multipliers"]
    assert d.book.s["lab_day"] == "2026-10-28"


# ---------- lean mode (10-31) ----------
def _capture_cli(monkeypatch):
    import subprocess
    seen = []

    class R:
        returncode = 0
        stdout = CLI_JSON
        stderr = ""

    def fake_run(args, **kw):
        seen.append({"args": list(args), "env": kw["env"], "input": kw["input"]})
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_lean_mode_passes_switches_and_replaces_the_system_prompt_for_trading_calls(monkeypatch):
    import brain as B
    seen = _capture_cli(monkeypatch)
    lean = {"args": ["--strict-mcp-config", "--disable-slash-commands"],
            "env": {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": 1}, "replace_system_prompt": True}
    B.ClaudeCLIBrain("claude", "opus", lean=lean).decide("hello")
    a, env, inp = seen[0]["args"], seen[0]["env"], seen[0]["input"]
    assert "--strict-mcp-config" in a and "--disable-slash-commands" in a
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and "ANTHROPIC_API_KEY" not in env
    assert "--bare" not in a                                    # bare mode needs an API key: never
    f = a[a.index("--system-prompt-file") + 1]
    assert open(f, encoding="utf-8").read() == B.SYSTEM         # desk's instructions became the system prompt
    assert B.SYSTEM[:60] not in inp and "hello" in inp          # and are not repeated in the message


def test_lean_research_keeps_claude_codes_own_prompt(monkeypatch):
    import brain as B
    seen = _capture_cli(monkeypatch)
    lean = {"args": ["--strict-mcp-config"], "replace_system_prompt": True}
    try:
        B.ClaudeCLIBrain("claude", "opus", lean=lean).research("go")
    except Exception:
        pass                                                    # the fake answer isn't research JSON; args matter
    a = seen[0]["args"]
    assert "--strict-mcp-config" in a and "--system-prompt-file" not in a
    assert B.RESEARCH_SYSTEM[:60] in seen[0]["input"]


def test_no_lean_config_means_calls_unchanged(monkeypatch):
    import brain as B
    seen = _capture_cli(monkeypatch)
    B.ClaudeCLIBrain("claude", "opus").decide("hello")
    a = seen[0]["args"]
    assert "--strict-mcp-config" not in a and "--system-prompt-file" not in a
    assert B.SYSTEM[:60] in seen[0]["input"]


def test_token_probe_runs_every_variant_and_saves(monkeypatch, tmp_path):
    import token_probe
    seen = _capture_cli(monkeypatch)
    monkeypatch.setattr(token_probe, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(token_probe, "HERE", str(tmp_path))
    (tmp_path / "data").mkdir()
    import shutil
    shutil.copy(os.path.join(HERE, "config.json"), tmp_path / "config.json")
    token_probe.main()
    assert len(seen) == len(token_probe.VARIANTS) == 5
    rows = json.load(open(tmp_path / "data" / "token_probe.json"))["rows"]
    assert [r["variant"] for r in rows] == ["A", "B", "C", "D", "E"]
    assert all("--bare" not in s["args"] for s in seen)


def test_config_lean_mode_is_exactly_the_measured_winner_and_never_bare():
    import token_probe
    lean = CFG["claude_lean"]
    assert lean == token_probe.VARIANTS[-1][2]        # what runs is what TOKEN_PROBE measured (variant E)
    assert "--bare" not in lean["args"]


def test_decision_numbers_written_like_a_person_still_parse():
    from brain import parse_decision, to_price
    assert to_price("$1,234.50") == 1234.5 and to_price(99) == 99.0
    assert to_price("N/A") is None and to_price("15%") is None and to_price("") is None and to_price(True) is None
    d = parse_decision('{"action":"pass","stop":"N/A","target":"none","why":"chop"}')
    assert d["action"] == "pass" and d["stop"] is None and d["target"] is None
    d = parse_decision('{"action":"buy","shares":"4","stop":"$571.80","target":"585","why":"x"}')
    assert d["shares"] == 4 and d["stop"] == 571.8 and d["target"] == 585.0


def test_a_buy_with_an_unreadable_stop_is_still_refused(tmp_path):
    bars = breakout_set()
    d, _ = make_desk(tmp_path, bars, [{"action": "buy", "shares": 10, "stop": "N/A", "target": 103.0, "why": "no stop"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert not d.book.s["positions"] and d.recent[-1]["result"].startswith("refused")


def test_every_decision_is_version_stamped_and_its_prompt_archived(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "pass", "why": "meh"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    rec = d.recent[-1]
    assert set(rec["v"]) == {"sys", "playbook", "scanner", "lean"} and rec["v"]["playbook"].startswith("none:")
    files = os.listdir(tmp_path / "data" / "prompts")
    rows = [json.loads(l) for l in open(tmp_path / "data" / "prompts" / files[0], encoding="utf-8")]
    assert rows[0]["id"] == rec["prompt_id"] and rows[0]["prompt"] == brain.prompts[0] and "meh" in rows[0]["reply"]


# ---------- 10-31 fill-honesty fixes ----------
def test_stop_touched_late_in_a_forming_minute_is_still_filled(tmp_path):
    """Regression (two code reviews, reproduced on the real 10-29/10-30 trades): the forming minute used to be
    marked 'checked' on first sight, so a stop broken later in that minute was never filled."""
    from engine import Book
    b = Book(str(tmp_path), CFG)
    t0 = ts(11, 0)
    assert b.buy("AMD", 100.0, 10, 99.0, 105.0, "x", t0 + 5, ["AMD"]) is None
    assert b.check_exits("AMD", [bar(t0 + 60, 100, 100.2, 99.6, 99.8)], now=t0 + 80) is None   # forming, low 99.6
    tr = b.check_exits("AMD", [bar(t0 + 60, 100, 100.2, 98.8, 99.9)], now=t0 + 125)           # finished, low 98.8
    assert tr and tr["why_out"].startswith("stop hit") and "AMD" not in b.s["positions"]
    assert tr["exit"] == round(99.0 * (1 - COST_PER_SIDE), 4)


def test_finished_minutes_are_checked_once_and_forming_ones_again(tmp_path):
    from engine import Book
    b = Book(str(tmp_path), CFG)
    t0 = ts(11, 0)
    b.buy("AMD", 100.0, 10, 99.0, 105.0, "x", t0 + 5, ["AMD"])
    b.check_exits("AMD", [bar(t0 + 60, 100, 100.2, 99.6, 99.8)], now=t0 + 80)
    assert b.s["last_bar_t"]["AMD"] == t0 + 5                 # forming bar not marked
    b.check_exits("AMD", [bar(t0 + 60, 100, 100.2, 99.6, 99.8)], now=t0 + 121)
    assert b.s["last_bar_t"]["AMD"] == t0 + 60                # finished bar marked


def test_scanner_and_claude_see_finished_minutes_only(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "pass", "why": "meh"}])
    d.step(bars["AMD"][-1]["t"] + 5)                          # breakout bar still forming: no wake-up
    assert brain.prompts == []
    d.step(bars["AMD"][-1]["t"] + 65)                         # now it has finished
    assert len(brain.prompts) == 1


class _MovingFeed(FakeFeed):
    """First call: the bars the scanner saw. Later calls: price has moved to `after` (or raises / goes stale)."""
    def __init__(self, bars, after=None, fail=False, stale=False):
        super().__init__(bars=bars)
        self.calls, self.after, self.fail, self.stale = 0, after, fail, stale

    def bars_1m(self, syms):
        self.calls += 1
        if self.calls == 1:
            return super().bars_1m(syms)
        if self.fail:
            raise RuntimeError("yahoo down")
        out = super().bars_1m(syms)
        for s in syms:
            if out.get(s) and self.after is not None and not self.stale:
                last = out[s][-1]
                out[s].append(bar(last["t"] + 60, self.after, self.after, self.after, self.after))
        return out


def _desk_with_feed(tmp_path, feed, answer):
    from brain import FakeBrain
    d = day_desk.Desk(str(tmp_path), dict(CFG, research_minute=10 ** 6), feed, FakeBrain([answer]))
    d.set_watchlist(["AMD"])
    return d


BUY = {"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "breakout"}


def test_buy_fills_at_the_price_after_claude_answers_and_logs_the_drift(tmp_path):
    bars = breakout_set()
    d = _desk_with_feed(tmp_path, _MovingFeed(bars, after=100.8), BUY)
    d.step(bars["AMD"][-1]["t"] + 65)
    rec = d.recent[-1]
    assert rec["ask_price"] == 100.5 and rec["fill_price"] == 100.8 and rec["drift_bps"] == round((100.8 / 100.5 - 1) * 1e4, 1)
    assert d.book.s["positions"]["AMD"]["entry"] == round(100.8 * (1 + COST_PER_SIDE), 4)


def test_buy_refused_when_the_price_has_already_fallen_through_the_stop(tmp_path):
    bars = breakout_set()
    d = _desk_with_feed(tmp_path, _MovingFeed(bars, after=98.9), BUY)
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "AMD" not in d.book.s["positions"] and d.recent[-1]["result"].startswith("refused")


def test_buy_refused_when_the_feed_fails_after_the_answer(tmp_path):
    bars = breakout_set()
    d = _desk_with_feed(tmp_path, _MovingFeed(bars, fail=True), BUY)
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "AMD" not in d.book.s["positions"] and d.recent[-1]["result"].startswith("refused: couldn't re-check")


def test_fresh_price_refuses_stale_data():
    import time as _t
    bars = breakout_set()
    d = day_desk.Desk.__new__(day_desk.Desk)
    d.cfg, d.feed, d.prices = CFG, FakeFeed(bars=bars), {}
    last = bars["AMD"][-1]["t"]
    px, _, err = d.fresh_price("AMD", last + 100, _t.time())
    assert px == bars["AMD"][-1]["c"] and err is None
    px, _, err = d.fresh_price("AMD", last + CFG["max_fill_staleness_s"] + 1, _t.time())
    assert px is None and "old" in err


# ---------- start-up guards ----------
def test_changed_or_missing_frozen_limit_pauses_new_buys_and_shows_why(tmp_path):
    cfg = dict(CFG)
    cfg.pop("max_daily_loss_pct")
    cfg["max_position_usd"] = 5000
    d = day_desk.Desk(str(tmp_path), cfg, FakeFeed(bars={}), None)
    assert d.book.s["paused"] is True
    assert "LIMITS CHANGED" in d.alerts[0] and "max_daily_loss_pct" in d.alerts[0] and "max_position_usd" in d.alerts[0]
    assert d.snapshot()["alerts"] == d.alerts and "ATTENTION" in d.health.heartbeat()["verdict"]


def test_frozen_limits_match_the_shipped_config():
    from engine import limits_problems
    assert limits_problems(CFG) == []


def test_position_already_sold_before_a_crash_is_not_sold_twice(tmp_path):
    from engine import Book
    b = Book(str(tmp_path), CFG)
    t0 = ts(11, 0)
    b.buy("AMD", 100.0, 10, 99.0, 105.0, "x", t0, ["AMD"])
    saved = json.load(open(tmp_path / "data" / "state.json"))      # state as it was BEFORE the sale...
    b.sell("AMD", 101.0, t0 + 600, "test")                          # ...trade row written...
    json.dump(saved, open(tmp_path / "data" / "state.json", "w"))   # ...crash: state.json never got the sale
    b2 = Book(str(tmp_path), CFG)
    assert "AMD" not in b2.s["positions"] and b2.repaired and len(b2.trades()) == 1
    assert b2.books_problem() is None


def test_books_mismatch_is_reported(tmp_path):
    d = day_desk.Desk(str(tmp_path), CFG, FakeFeed(bars={}), None)
    d.book.s["cash"] += 50
    d.book.save()
    d2 = day_desk.Desk(str(tmp_path), CFG, FakeFeed(bars={}), None)
    assert any("BOOKS DON'T ADD UP" in a for a in d2.alerts) and d2.book.s["paused"] is False


def test_early_close_day_means_no_buys_and_no_research(tmp_path):
    bars = breakout_set()
    day = et_now(bars["AMD"][-1]["t"]).date().isoformat()
    from brain import FakeBrain
    brain = FakeBrain([BUY])
    d = day_desk.Desk(str(tmp_path), dict(CFG, no_trade_days={day: "early close"}), FakeFeed(bars=bars), brain)
    d.set_watchlist(["AMD"])
    d._sync_research = True
    d.step(bars["AMD"][-1]["t"] + 65)
    assert brain.prompts == [] and not d.book.s["positions"] and "no trading today" in d.status["market"]
    d.maybe_morning_research(ts(8, 50))
    assert d.book.s.get("research_day") != day                # research never started on a no-trade day


def test_shipped_config_sits_out_the_2026_early_closes():
    assert set(CFG["no_trade_days"]) >= {"2026-11-27", "2026-12-24"}


# ---------- re-entry lock after a stop ----------
def _stopped_desk(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [BUY, BUY, BUY])
    t_buy = bars["AMD"][-1]["t"] + 65
    d.step(t_buy)                                                        # buys AMD, stop 99
    assert "AMD" in d.book.s["positions"]
    crash = {"AMD": bars["AMD"] + [bar(bars["AMD"][-1]["t"] + 120, 100.2, 100.3, 98.5, 98.7)],
             "QQQ": bars["QQQ"] + [bar(bars["QQQ"][-1]["t"] + 120, 500, 500, 500, 500)]}
    d.feed = FakeFeed(bars=crash)
    d.step(crash["AMD"][-1]["t"] + 65)                                   # stop fills
    assert "AMD" not in d.book.s["positions"]
    return d, brain, crash


def test_a_stop_out_locks_the_stock_for_the_configured_minutes(tmp_path):
    d, brain, crash = _stopped_desk(tmp_path)
    t_stop = d.book.trades()[-1]["exit_t"]
    assert d.cool.locked("AMD", t_stop + 59 * 60) and not d.cool.locked("AMD", t_stop + 61 * 60)
    assert not d.cool.ready("AMD", "breakout", t_stop + 30 * 60)
    assert d.recent[-1]["trigger"] in ("re-entry lock", "resting order")
    assert any(r["trigger"] == "re-entry lock" for r in d.recent)
    assert "AMD" in d.snapshot()["locks"] or d.cool.locked("AMD", time.time()) is None   # page shows live locks


def test_lock_survives_a_restart(tmp_path):
    d, _, _ = _stopped_desk(tmp_path)
    t_stop = d.book.trades()[-1]["exit_t"]
    d2 = day_desk.Desk(str(tmp_path), d.cfg, FakeFeed(bars={}), None)
    assert d2.cool.locked("AMD", t_stop + 10 * 60)


def test_second_stop_in_a_day_locks_until_tomorrow(tmp_path):
    from engine import Book
    d, _, _ = _stopped_desk(tmp_path)
    t = d.book.trades()[-1]["exit_t"]
    d.book.buy("AMD", 100.0, 1, 99.0, 105.0, "x", t + 3000, ["AMD"])
    tr = d.book.sell("AMD", 99.0, t + 3600, "stop hit 12:00 PM")
    d.lock_after_stop("AMD", tr, et_now(t))
    assert d.cool.locked("AMD", t + 5 * 3600) and "until tomorrow" in d.cool.locked("AMD", t + 5 * 3600)


def test_engine_rows_for_locks_are_not_counted_as_claude_calls():
    import grade_read
    assert "re-entry lock" in day_desk.NOT_CLAUDE_CALLS and grade_read.NOT_CLAUDE_CALLS == day_desk.NOT_CLAUDE_CALLS


# ---------- rule credit (10-31) ----------
def test_rule_ids_are_stable_and_never_reused():
    from day_desk import assign_rule_ids
    old = [{"id": "R1"}, {"id": "R2"}, {"id": "R5"}]
    new = [{"id": "R2"}, {"id": None}, {"id": "R9"}, {"id": "R2"}]
    top = assign_rule_ids(new, old, last_id=5)
    assert [l["id"] for l in new] == ["R2", "R6", "R7", "R8"] and top == 8   # unknown R9 and a duplicate get fresh ids
    again = [{"id": None}]
    assert assign_rule_ids(again, [{"id": "R2"}], last_id=8) == 9 and again[0]["id"] == "R9"   # R3..R8 never reused


def test_decisions_cite_rules_and_solid_is_earned_in_code(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [dict(BUY, rules=["R1", "bogus", "r2"])])
    write = day_desk.write_json
    write(d.lessons_path, {"day": "2026-10-27", "summary": "", "lessons": [
        {"rule": "wait for volume", "evidence": "x", "status": "tentative"},
        {"rule": "no chop", "evidence": "y", "status": "tentative"}]}, indent=1)
    assert [l["id"] for l in d.lessons()["lessons"]] == ["R1", "R2"]          # old playbook gets ids once
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "R1 [tentative] wait for volume" in brain.prompts[0]
    assert d.recent[-1]["rules"] == ["R1", "R2"]
    brain.review_answer = {"summary": "s", "lessons": [
        {"id": "R1", "rule": "wait for volume", "evidence": "AMD", "status": "solid"},
        {"id": "new", "rule": "fresh idea", "evidence": "z", "status": "tentative"}]}
    late = {"AMD": bars["AMD"] + flat_bars(start_h=15, start_m=44, n=3), "QQQ": bars["QQQ"] + flat_bars(start_h=15, start_m=44, n=3, price=500.0)}
    d.feed = FakeFeed(bars=late)
    d.step(ts(15, 46))
    assert "RULE CREDIT" in brain.review_prompts[0] and "R1: cited 1x" in brain.review_prompts[0]
    L = d.lessons()["lessons"]
    assert L[0]["id"] == "R1" and L[0]["status"] == "tentative" and "code: kept tentative" in L[0]["evidence"]
    assert L[1]["id"] == "R3"                                                    # new rule, R1/R2 never reused
    c = d.rule_credit()["R1"]
    assert c["cited"] == 1 and c["trades"] == 1 and c["days"] == 1


def test_position_up_one_risk_unit_asks_claude_once_to_protect_the_gain(tmp_path):
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [BUY, {"action": "adjust", "stop": 100.4, "why": "lock in breakeven"}])
    d.step(bars["AMD"][-1]["t"] + 65)                                   # buys at ~100.5, stop 99 -> risk ~1.5
    up = {"AMD": bars["AMD"] + [bar(bars["AMD"][-1]["t"] + 120, 100.6, 102.2, 100.6, 102.1)],
          "QQQ": bars["QQQ"] + [bar(bars["QQQ"][-1]["t"] + 120, 500, 500, 500, 500)]}
    d.feed = FakeFeed(bars=up)
    d.step(up["AMD"][-1]["t"] + 65)
    assert d.recent[-1]["trigger"] == "protect-gain" and d.book.s["positions"]["AMD"]["stop"] == 100.4
    assert d.book.s["positions"]["AMD"]["stop0"] == 99.0
    n = len(brain.prompts)
    d.step(up["AMD"][-1]["t"] + 95)                                     # still up: never asked twice
    assert len(brain.prompts) == n


def test_base_rates_reach_fresh_decisions_only(tmp_path):
    """10-31 weekend: the trigger lab and decision score reached Claude only in the evening review. Now every fresh
    buy/pass prompt carries them; held positions don't; missing or broken reports never block a call."""
    import json as _j
    bars = breakout_set()
    d, brain = make_desk(tmp_path, bars, [{"action": "pass", "why": "x"}])
    assert d.base_rates("breakout", False) == ""                       # no reports yet: nothing, no crash
    D = tmp_path / "data"
    (D / "trigger_lab.json").write_text(_j.dumps({"rows": [{"trigger": "breakout", "days": 21, "n_r": 100, "mean_r": -0.03,
                                                             "random_mean_r": 0.06, "verdict": "within noise"}]}))
    (D / "decision_score.json").write_text(_j.dumps({"n": 40, "days": ["a", "b", "c"], "by_action": {
        "buy": {"n": 10, "mean_r": -0.53}, "pass": {"n": 30, "mean_r": -0.06}}, "buy_minus_pass": {"verdict": "too few days"}}))
    txt = d.base_rates("breakout", False)
    assert "-0.03 R" in txt and "+0.06 R" in txt and "buys averaged -0.53 R" in txt
    assert d.base_rates("breakout", True) == ""                        # holding: not about entries
    d.step(bars["AMD"][-1]["t"] + 65)
    assert "Base rates" in brain.prompts[-1] and "breakout" in brain.prompts[-1]
    (D / "trigger_lab.json").write_text("{broken")
    import os as _o, time as _t
    _o.utime(D / "trigger_lab.json", (_t.time() + 5, _t.time() + 5))
    assert "buys averaged" in d.base_rates("breakout", False)          # one bad report doesn't kill the other
    d.cfg = dict(d.cfg, base_rates_in_prompt=False)
    assert d.base_rates("breakout", False) == ""
