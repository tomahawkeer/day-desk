"""Token ceiling guard (11-01, config "token_ceiling", promised on anthropics/claude-code#106413).
A Claude call whose input tokens per turn are far above its kind's ceiling gets flagged: the row carries
token_flag, the page shows an amber banner for the day, and only the FIRST flag of the day sends a phone alert.
It must never block a trade or stop a row being logged. Synthetic data only, no network."""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import FakeBrain                                      # noqa: E402
from engine import ET                                            # noqa: E402
from feed import FakeFeed                                        # noqa: E402
import day_desk                                                  # noqa: E402
import health                                                    # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = day_desk.load_config(HERE)
BLOAT = 429_388                     # the measured 10-31 failure: a one-line call loading every connector upfront


def ts(h, m, day=2):
    return datetime(2026, 11, day, h, m, tzinfo=ET).timestamp()


class RecNotifier:
    def __init__(self):
        self.sent = []

    def send(self, event, title, body):
        self.sent.append((event, title, body))
        return True

    def status(self):
        return {"enabled": True}


def desk(tmp_path, cfg=None, notifier=None):
    return day_desk.Desk(str(tmp_path), dict(cfg or CFG, research_minute=10 ** 6), FakeFeed(bars={}), None,
                         notifier or RecNotifier())


def row(trigger="breakout", tok_in=3000, turns=1, t=None, sym="AMD"):
    return {"t": t or ts(10, 0), "sym": sym, "trigger": trigger, "action": "pass", "result": "no trade",
            "usage": {"tok_in": tok_in, "tok_out": 50, "cost_usd": 0.01, "turns": turns}}


def rows(tmp_path):
    with open(os.path.join(tmp_path, "data", "decisions.jsonl"), encoding="utf-8") as f:
        return [json.loads(l) for l in f]


def test_config_block_exists_with_comment_and_sits_between_normal_and_the_known_failure():
    tc = CFG["token_ceiling"]
    assert CFG.get("_token_ceiling") and tc["enabled"] is True
    c = tc["per_turn_tok_in"]
    assert set(c) == {"trading", "review", "self-test", "research"}
    for kind, normal in {"trading": 10_000, "review": 10_000, "self-test": 600, "research": 11_000}.items():
        assert normal < c[kind] < BLOAT, kind           # normal never rings; the 10-31 failure always does
    assert CFG["alerts"]["events"]["token_ceiling"] in ("loud", "normal", "silent", "off")


def test_call_kind_is_shared_with_the_usage_summary():
    assert [day_desk.call_kind(t) for t in ("morning research", "end-of-day review", "self-test", "breakout")] == \
        ["research", "review", "self-test", "trading"]


def test_normal_calls_are_not_flagged(tmp_path):
    d = desk(tmp_path)
    for r in (row(tok_in=4000), row("end-of-day review", 12_000), row("self-test", 600),
              row("morning research", 208_545, turns=20)):      # research: big total, ~10k per turn = normal
        d.record(r)
    assert not any("token_flag" in r for r in rows(tmp_path))
    assert d.token_banner(ts(10, 5)) == "" and d.notifier.sent == []


def test_bloated_call_is_flagged_logged_bannered_and_alerts_once(tmp_path):
    d = desk(tmp_path)
    d.record(row(tok_in=BLOAT))
    d.record(row(tok_in=BLOAT, t=ts(10, 30), sym="NVDA"))
    rs = rows(tmp_path)
    assert len(rs) == 2 and all(r["token_flag"]["per_turn"] == BLOAT for r in rs)   # every row still logged
    assert rs[0]["token_flag"]["kind"] == "trading" and rs[0]["token_flag"]["ceiling"] == CFG["token_ceiling"]["per_turn_tok_in"]["trading"]
    ev = [e for e, _, _ in d.notifier.sent]
    assert ev == ["token_ceiling"]                               # ONE alert a day, not one per call
    b = d.token_banner(ts(10, 31))
    assert "TOKEN CEILING: 2 Claude call(s)" in b and "429,388" in b
    assert "token_banner" in d.snapshot()


def test_research_is_judged_per_turn(tmp_path):
    d = desk(tmp_path)
    d.record(row("morning research", BLOAT * 20, turns=20))    # every turn bloated = flagged
    assert rows(tmp_path)[-1]["token_flag"]["kind"] == "research"


def test_alert_once_per_day_survives_a_restart_and_resets_next_day(tmp_path):
    d = desk(tmp_path)
    d.record(row(tok_in=BLOAT))
    d2 = desk(tmp_path)                                          # program restarted: state.json remembers
    d2.record(row(tok_in=BLOAT, t=ts(11, 0)))
    assert d2.notifier.sent == [] and d2.book.s["token_flags"]["count"] == 2
    d2.record(row(tok_in=BLOAT, t=ts(10, 0, day=3)))             # next day: counts again from 1, rings once
    assert [e for e, _, _ in d2.notifier.sent] == ["token_ceiling"]
    assert d2.book.s["token_flags"]["count"] == 1
    assert d2.token_banner(ts(10, 0, day=2)) == ""               # yesterday's banner is gone


def test_engine_rows_and_rows_without_usage_are_ignored(tmp_path):
    d = desk(tmp_path)
    d.record({"t": ts(10, 0), "sym": "AMD", "trigger": "resting order", "action": "sell", "result": "x",
              "usage": {"tok_in": BLOAT}})
    d.record({"t": ts(10, 0), "sym": "AMD", "trigger": "breakout", "action": "error", "result": "failed"})
    d.record({"t": ts(10, 0), "sym": "AMD", "trigger": "breakout", "action": "pass", "usage": None})
    assert not any("token_flag" in r for r in rows(tmp_path)) and d.notifier.sent == []


def test_disabled_guard_flags_nothing(tmp_path):
    cfg = dict(CFG, token_ceiling=dict(CFG["token_ceiling"], enabled=False))
    d = desk(tmp_path, cfg)
    d.record(row(tok_in=BLOAT))
    assert "token_flag" not in rows(tmp_path)[0] and d.notifier.sent == []


def test_a_broken_guard_or_notifier_never_stops_a_row_being_logged(tmp_path):
    class Boom(RecNotifier):
        def send(self, *a):
            raise RuntimeError("ntfy down")
    d = desk(tmp_path, notifier=Boom())
    d.record(row(tok_in=BLOAT))                                  # notifier raises: row still logged
    d.token_check = lambda rec: 1 / 0                            # the guard itself crashes: row still logged
    d.record(row(tok_in=BLOAT, t=ts(10, 5)))
    assert len(rows(tmp_path)) == 2


def test_a_real_trading_call_through_ask_is_checked(tmp_path):
    class BloatedBrain(FakeBrain):
        def last_usage(self):
            return {"tok_in": BLOAT, "tok_out": 20, "cost_usd": 0.12, "turns": 1}
    t0 = ts(10, 0)
    amd = [{"t": t0 + 60 * i, "o": 100.0, "h": 100.05, "l": 99.95, "c": 100.0, "v": 1000} for i in range(40)]
    amd.append({"t": amd[-1]["t"] + 60, "o": 100.0, "h": 100.6, "l": 100.0, "c": 100.5, "v": 8000})
    qqq = [dict(b, o=500.0, h=500.05, l=499.95, c=500.0) for b in amd]
    brain = BloatedBrain([{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0, "why": "breakout"}])
    n = RecNotifier()
    d = day_desk.Desk(str(tmp_path), dict(CFG, research_minute=10 ** 6), FakeFeed(bars={"AMD": amd, "QQQ": qqq}),
                      brain, n)
    d.set_watchlist(["AMD"])
    d.step(amd[-1]["t"] + 65)
    assert "AMD" in d.book.s["positions"]                        # the guard never blocks a trade
    asked = [r for r in rows(tmp_path) if r.get("trigger") not in day_desk.NOT_CLAUDE_CALLS]
    assert asked and asked[-1]["token_flag"]["tok_in"] == BLOAT
    assert [e for e, _, _ in n.sent].count("token_ceiling") == 1


def test_heartbeat_reports_todays_count_without_touching_the_verdict(tmp_path):
    d = desk(tmp_path)
    now = ts(10, 0)
    d.record(row(tok_in=BLOAT, t=now))
    real = health.time
    try:
        hb = d.health.heartbeat(now)
    finally:
        health.time = real
    assert hb["token_flags_today"] == 1 and "ATTENTION" not in hb["verdict"]
