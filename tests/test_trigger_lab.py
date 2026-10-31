"""Tests for the trigger lab (trigger_lab.py) and the 30-day backfill. Run TEST.bat (or: python -m pytest -q tests)."""
import copy
import json
import os
import sys
from datetime import date, datetime

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backfill_bars                                              # noqa: E402
import scanner                                                    # noqa: E402
import trigger_lab as tl                                          # noqa: E402
from engine import COST_PER_SIDE, ET                              # noqa: E402

CFG = tl.load_config(ROOT)
DAY = "2026-10-28"


def ts(h, m, day=28):
    return datetime(2026, 10, day, h, m, tzinfo=ET).timestamp()


def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat_day(price=100.0, rng=0.2, day=28, start=(9, 30), end=(16, 0)):
    """One bar per minute, open=close=price, high/low +-rng/2."""
    out = []
    m = start[0] * 60 + start[1]
    while m < end[0] * 60 + end[1]:
        out.append(bar(ts(m // 60, m % 60, day), price, price + rng / 2, price - rng / 2, price))
        m += 1
    return out


# ---------------- 1) replay ----------------
def test_replay_uses_completed_bars_only(monkeypatch):
    seen = []

    def spy(sym, bars, qqq, cfg):
        seen.append((sym, bars[-1]["t"] if bars else None, qqq[-1]["t"] if qqq else None))
        return []
    monkeypatch.setattr(tl, "scan", spy)
    db = {"AAA": flat_day(), "QQQ": flat_day(400)}
    tl.replay_day(db, CFG, ["AAA"])
    assert seen
    for minute_i, (_s, last_t, q_t) in enumerate(seen):
        tick_minute = tl.MARKET_OPEN_MINUTE + 1 + minute_i
        tick_t = tl.minute_ts(DAY, tick_minute)
        assert last_t < tick_t and last_t == tick_t - 60      # the newest bar it sees is the one that just completed
        assert q_t == last_t                                  # QQQ lined up by time
    assert len(seen) == CFG["flatten_minute"] - tl.MARKET_OPEN_MINUTE - 1   # scanned once per minute, until 3:45


def test_replay_uses_real_cooldowns_and_per_tick_limit(monkeypatch):
    monkeypatch.setattr(tl, "scan", lambda sym, bars, qqq, cfg: [("momentum", "m"), ("breakout", "b")])
    db = {s: flat_day() for s in ("AAA", "BBB", "CCC", "QQQ")}
    st = {}
    fires = tl.replay_day(db, CFG, ["AAA", "BBB", "CCC"], st)
    sc = CFG["scanner"]
    first = [f for f in fires if f[2] == tl.minute_ts(DAY, 571)]
    assert [(f[0], f[1]) for f in first] == [("AAA", "momentum"), ("BBB", "momentum")]   # 2 per tick
    second = [f for f in fires if f[2] == tl.minute_ts(DAY, 571) + CFG["poll_seconds"]]
    assert [(f[0], f[1]) for f in second] == [("CCC", "momentum")]      # crowded-out stock gets the next tick
    assert st["crowded_out"] >= 1
    aaa = [f for f in fires if f[0] == "AAA"]
    gaps = [b[2] - a[2] for a, b in zip(aaa, aaa[1:])]
    assert min(gaps) >= sc["cooldown_symbol_min"] * 60                   # per-stock cooldown honoured
    mom = [f[2] for f in aaa if f[1] == "momentum"]
    assert min(b - a for a, b in zip(mom, mom[1:])) >= sc["cooldown_same_trigger_min"] * 60
    assert aaa[1][1] == "breakout"                                       # momentum still cooling -> next kind
    assert all(tl.minutes_et(f[2]) < CFG["flatten_minute"] for f in fires)


def test_replay_matches_real_scanner_on_fresh_cooldowns():
    """With one stock and no crowding, replay = 'scan completed bars + Cooldowns' done by hand."""
    import random
    rnd = random.Random(3)
    bars, p = [], 100.0
    for i in range(390):
        o = p; c = o * (1 + rnd.uniform(-0.006, 0.0065))
        bars.append(bar(ts(9, 30) + 60 * i, o, max(o, c) * 1.001, min(o, c) * 0.999, c, rnd.randint(500, 3000)))
        p = c
    q = flat_day(400)
    fires = tl.replay_day({"AAA": bars, "QQQ": q}, CFG, ["AAA"])
    cool = scanner.Cooldowns(CFG, path=None)
    expect = []
    for minute in range(571, CFG["flatten_minute"]):
        for k in range(2):
            now = tl.minute_ts(DAY, minute) + 30 * k
            done = [b for b in bars if b["t"] < tl.minute_ts(DAY, minute)]
            for kind, why in scanner.scan("AAA", done, [b for b in q if b["t"] < tl.minute_ts(DAY, minute)], CFG):
                if cool.ready("AAA", kind, now):
                    cool.mark("AAA", kind, now); expect.append(("AAA", kind, now, why))
                    break
    assert fires == expect and fires


# ---------------- 2) shadow trade ----------------
def setup_bars(after):
    """30 flat morning bars (range 1.0 on 100 -> typical 1%), entry bar at 10:00 opening 100, then `after`."""
    pre = [bar(ts(9, 30) + 60 * i, 100, 100.5, 99.5, 100) for i in range(30)]
    entry = bar(ts(10, 0), 100, 100.2, 99.9, 100)
    rest = [bar(ts(10, 1) + 60 * i, *x) for i, x in enumerate(after)]
    return pre + [entry] + rest


QQQ = flat_day(400)


def test_shadow_stop_first_and_clamp():
    b = setup_bars([(100, 100.1, 97.5, 98)])
    r = tl.shadow_trade(b, QQQ, ts(10, 0), CFG)
    assert r["entry"] == 100 and r["stop"] == pytest.approx(98.0)       # 2 x 1% typical = 2% (< 3% cap)
    assert r["target"] == pytest.approx(103.0)
    assert r["exit_reason"] == "stop" and not r["target_first"]
    assert r["r"] == pytest.approx((98 * (1 - COST_PER_SIDE) - 100 * (1 + COST_PER_SIDE)) / 2)
    wide = [bar(ts(9, 30) + 60 * i, 100, 102, 98, 100) for i in range(30)] + b[30:]
    assert tl.shadow_trade(wide, QQQ, ts(10, 0), CFG)["stop"] == pytest.approx(100 * (1 - CFG["max_stop_pct"] / 100))


def test_shadow_target_first_and_both_in_one_bar_is_stop():
    r = tl.shadow_trade(setup_bars([(100, 101, 99.5, 100.5), (100.5, 103.2, 100, 103)]), QQQ, ts(10, 0), CFG)
    assert r["exit_reason"] == "target" and r["exit"] == pytest.approx(103.0) and r["target_first"]
    assert r["r"] == pytest.approx((103 * (1 - COST_PER_SIDE) - 100 * (1 + COST_PER_SIDE)) / 2)
    both = tl.shadow_trade(setup_bars([(100, 104, 97, 100)]), QQQ, ts(10, 0), CFG)
    assert both["exit_reason"] == "stop"


def test_shadow_gap_fills_at_open():
    r = tl.shadow_trade(setup_bars([(100, 100.1, 99.8, 100), (96.5, 97, 96, 96.8)]), QQQ, ts(10, 0), CFG)
    assert r["exit_reason"] == "stop" and r["exit"] == 96.5 and r["r"] < -1.7
    up = tl.shadow_trade(setup_bars([(104, 105, 103.9, 104.5)]), QQQ, ts(10, 0), CFG)
    assert up["exit_reason"] == "target" and up["exit"] == 104


def test_shadow_flatten_exit_and_entry_bar_skipped():
    after = [(100, 100.3, 99.7, 100.5)] * 400                          # wanders but never hits; runs past 3:45
    b = setup_bars(after)
    r = tl.shadow_trade(b, QQQ, ts(10, 0), CFG)
    assert r["exit_reason"] == "close-out" and r["exit"] == 100.5
    assert tl.minutes_et(r["exit_t"]) == CFG["flatten_minute"]          # close of the 3:44 bar
    b[30] = bar(ts(10, 0), 100, 110, 90, 100)                           # entry bar itself is not walked (engine rule)
    assert tl.shadow_trade(b, QQQ, ts(10, 0), CFG)["exit_reason"] == "close-out"


def test_shadow_after_last_buy_and_tick2_entry():
    b = setup_bars([(100, 100.1, 99.9, 100)] * 380)
    assert tl.shadow_trade(b, QQQ, tl.minute_ts(DAY, CFG["last_buy_minute"]), CFG) is None
    r = tl.shadow_trade(b, QQQ, ts(10, 0) + 30, CFG)                   # tick 2: next bar to open is 10:01
    assert r["entry_t"] == ts(10, 1)


def test_forward_returns_vs_qqq():
    pre = [bar(ts(9, 30) + 60 * i, 100, 100.5, 99.5, 100) for i in range(30)]
    later = [bar(ts(10, 0) + 60 * i, 100, 100.2, 99.9, 101 if i >= 29 else 100) for i in range(59)]
    q = [bar(ts(9, 30) + 60 * i, 400, 400.1, 399.9, 402 if i >= 59 else 400) for i in range(90)]
    r = tl.shadow_trade(pre + later, q, ts(10, 0), CFG)
    assert r["fwd15"] == pytest.approx(0.0)
    assert r["fwd30"] == pytest.approx(1.0 - 0.5)                      # stock +1%, QQQ +0.5% at 10:30
    assert r["fwd60"] is None                                           # last bar is 10:58: ends before 11:00


# ---------------- 3) random baseline ----------------
def test_random_baseline_deterministic_and_in_window():
    a = tl.random_minutes(DAY, "AAA", "momentum", 1793200000, CFG)
    assert a == tl.random_minutes(DAY, "AAA", "momentum", 1793200000, CFG)
    assert a != tl.random_minutes(DAY, "AAA", "momentum", 1793200060, CFG)
    assert len(a) == tl.RANDOM_PER_FIRE
    assert all(CFG["scanner"]["start_minute"] <= m < CFG["last_buy_minute"] for m in a)
    b = flat_day()
    x = tl.random_labels(DAY, "AAA", "momentum", 1, b, QQQ, CFG)
    assert [y["r"] for y in x] == [y["r"] for y in tl.random_labels(DAY, "AAA", "momentum", 1, b, QQQ, CFG)]


# ---------------- 4) bootstrap ----------------
def test_day_block_bootstrap():
    # one day is a huge outlier: resampling whole days must sometimes include it and sometimes not
    fire = {"d1": [1.0] * 50, "d2": [0.0], "d3": [0.0]}
    rand = {"d1": [0.0], "d2": [0.0], "d3": [0.0]}
    lo, hi, n = tl.day_bootstrap(fire, rand)
    # whole-day blocks: ~30% of resamples miss d1 entirely, so the low end is exactly 0. Resampling single
    # observations would almost never draw only d2/d3 (2 of 52 fires) and the low end would sit near 0.9.
    assert n == tl.BOOT_N and lo == 0.0 and hi > 0.9
    assert tl.day_bootstrap(fire, rand) == (lo, hi, n)                 # seeded
    assert tl.day_bootstrap({}, {}) == (None, None, 0)
    lo2, hi2, _ = tl.day_bootstrap({"a": [1.0], "b": [1.2]}, {"a": [0.0], "b": [0.1]})
    assert lo2 > 0


# ---------------- folder fixtures ----------------
def make_folder(tmp_path, days=("2026-10-27", "2026-10-28"), live=None):
    data = tmp_path / "data"
    (data / "bars").mkdir(parents=True)
    cfg = copy.deepcopy(CFG)
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    (data / "watchlist.json").write_text(json.dumps({"symbols": ["AAA"]}))
    import random
    for d in days:
        dd = int(d[-2:])
        rnd = random.Random(dd)
        out = {}
        for s in ("AAA", "BBB"):
            p, bars = 100.0, []
            for i in range(390):
                o = p; c = o * (1 + rnd.uniform(-0.006, 0.0066))
                bars.append(bar(ts(9, 30, dd) + 60 * i, o, max(o, c) * 1.001, min(o, c) * 0.999, c, rnd.randint(500, 3000)))
                p = c
            out[s] = bars
        out["QQQ"] = flat_day(400, day=dd)
        (data / "bars" / f"{d}.json").write_text(json.dumps({"day": d, "bars": out}))
    with open(data / "decisions.jsonl", "w") as f:
        for r in live or []:
            f.write(json.dumps(r) + "\n")
    return str(tmp_path)


def test_confirm_stamp_refuses_rerun(tmp_path, capsys):
    folder = make_folder(tmp_path)
    assert tl.main(["--confirm-after", "2026-10-28"], folder) == 1           # no later days: no stamp
    assert not os.path.exists(tl.stamp_path(folder, "2026-10-28"))
    assert tl.main(["--confirm-after", "2026-10-27"], folder) == 0
    out = capsys.readouterr().out
    assert "CONFIRM RUN" in out and "2026-10-27 .." not in out                 # only days after the date
    assert os.path.exists(tl.stamp_path(folder, "2026-10-27"))
    rep = json.load(open(os.path.join(folder, "data", "trigger_lab_confirm_2026-10-27.json")))
    assert rep["days"] == ["2026-10-28"]
    assert tl.main(["--confirm-after", "2026-10-27"], folder) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert tl.main(["--confirm-after", "nonsense"], folder) == 2


def test_parity_matching(tmp_path):
    folder = make_folder(tmp_path)
    db = tl.load_day(folder, "2026-10-28")
    fires = tl.replay_day(db, CFG, ["AAA"])
    assert fires
    s, k, t, _ = fires[0]
    live = [{"t": t + 90, "sym": s, "trigger": k},                      # within 2 min -> match
            {"t": t, "sym": s, "trigger": "nope_kind"},                 # not a scanner kind: ignored
            {"t": t + 500, "sym": s, "trigger": "momentum" if k != "momentum" else "breakout"},   # miss
            {"t": t, "sym": "-", "trigger": "review"}]
    folder = make_folder(tmp_path / "b", live=live)
    par = tl.parity(folder, ["2026-10-27", "2026-10-28"])
    assert len(par) == 1 and par[0]["day"] == "2026-10-28"
    p = par[0]
    assert p["live"] == 2 and p["matched"] == 1 and len(p["misses"]) == 1
    assert p["symbols"] == ["AAA"] and p["forming_bar_era"]
    live[0]["t"] = t + 200                                              # outside 2 min -> miss
    folder = make_folder(tmp_path / "c", live=live)
    assert tl.parity(folder, ["2026-10-28"])[0]["matched"] == 0


def test_summary_length_cap_and_files(tmp_path):
    folder = make_folder(tmp_path)
    s = tl.summary_lines(folder)
    lines = s.split("\n")
    assert len(lines) <= 6 and lines[-1].startswith("Exploratory, n=2 days") and lines[-1].endswith("a clue, not proof.")
    assert os.path.exists(os.path.join(folder, "data", "trigger_lab.json"))
    assert os.path.exists(os.path.join(folder, "data", "trigger_lab.txt"))
    assert tl.summary_lines(folder) == s                                # served from the saved file the 2nd time
    txt = open(os.path.join(folder, "data", "trigger_lab.txt")).read()
    assert "EXPLORATORY on n=2 days" in txt and "confirm" in txt


def test_report_runs_watchlist_mode(tmp_path):
    folder = make_folder(tmp_path)
    rep = tl.analyse(folder, ["2026-10-27", "2026-10-28"], "watchlist")
    assert {f["sym"] for f in rep["fires"]} <= {"AAA"}
    assert [r["trigger"] for r in rep["rows"]] == list(tl.SCANNER_KINDS) + ["ALL"]
    assert rep["rows"][-1]["fires"] == len(rep["fires"])


# ---------------- 9) backfill ----------------
class FakeYF:
    """Stands in for yfinance: serves 1-minute bars for whatever [start, end) is asked, records each call."""

    def __init__(self):
        self.calls = []

    def download(self, tickers, **kw):
        self.calls.append(kw)
        assert kw["threads"] is False and kw["interval"] == "1m"
        if "period" in kw:
            s, e = date(2026, 10, 26), date(2026, 11, 3)
        else:
            s, e = date.fromisoformat(kw["start"]), date.fromisoformat(kw["end"])
            assert (e - s).days <= backfill_bars.CHUNK_DAYS
        idx, d = [], s
        while d < e:
            if d.weekday() < 5:
                for m in (9 * 60 + 29, 9 * 60 + 30, 9 * 60 + 31, 15 * 60 + 59, 16 * 60):   # 2 outside the session
                    idx.append(pd.Timestamp(datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=ET)))
            d = date.fromordinal(d.toordinal() + 1)
        cols = pd.MultiIndex.from_product([tickers.split(), ["Open", "High", "Low", "Close", "Volume"]])
        return pd.DataFrame([[1.0, 2.0, 0.5, 1.5, 100.0] * len(tickers.split())] * len(idx), index=idx, columns=cols)


def test_backfill_chunks():
    w = backfill_bars.chunks(date(2026, 10, 31), 30)
    assert w[0][0] == date(2026, 10, 2) and w[-1][1] == date(2026, 11, 1)
    assert all((e - s).days <= 7 for s, e in w) and all(a[1] == b[0] for a, b in zip(w, w[1:]))
    assert len(w) == 5
    assert backfill_bars.chunks(date(2026, 10, 31), 99)[0][0] == date(2026, 10, 2)   # capped at 30 days


def test_backfill_never_overwrites_never_today(tmp_path):
    data = tmp_path / "data"
    (data / "bars").mkdir(parents=True)
    keep = {"day": "2026-10-28", "bars": {"QQQ": []}, "source": "close-out"}
    (data / "bars" / "2026-10-28.json").write_text(json.dumps(keep))
    yf = FakeYF()
    msgs = []
    written = backfill_bars.run(days=30, yf=yf, today=date(2026, 10, 30), data_dir=str(data),
                                syms=["QQQ", "BRK.B"], report=msgs.append)
    assert len(yf.calls) == 5 and all("start" in c for c in yf.calls)          # 30 days in 5 single-threaded chunks
    assert "2026-10-28" not in written and "2026-10-30" not in written
    assert json.loads((data / "bars" / "2026-10-28.json").read_text()) == keep
    assert not (data / "bars" / "2026-10-30.json").exists()
    assert "2026-10-01" in written or "2026-10-02" in written
    f = json.loads((data / "bars" / "2026-10-29.json").read_text())
    assert f["bar_counts"] == {"BRK.B": 3, "QQQ": 3} and "--days 30" in f["source"]
    assert [tl.minutes_et(b["t"]) for b in f["bars"]["QQQ"]] == [570, 571, 959]
    again = backfill_bars.run(days=30, yf=FakeYF(), today=date(2026, 10, 30), data_dir=str(data),
                              syms=["QQQ"], report=msgs.append)
    assert again == []                                                        # rerun: nothing new, nothing overwritten


def test_backfill_default_is_single_7d_request(tmp_path):
    data = tmp_path / "data"
    yf = FakeYF()
    written = backfill_bars.run(yf=yf, today=date(2026, 10, 31), data_dir=str(data), syms=["QQQ"], report=lambda m: None)
    assert len(yf.calls) == 1 and yf.calls[0]["period"] == "7d"
    assert written == ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
    assert "period=7d" in json.loads((data / "bars" / "2026-10-30.json").read_text())["source"]
