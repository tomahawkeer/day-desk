"""Tests for the trade lab (lab.py). Run TEST.bat (or: python -m pytest -q tests)."""
import csv
import json
import os
import shutil
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import day_desk                                                   # noqa: E402
import lab                                                        # noqa: E402
from engine import Book, ET, COST_PER_SIDE, TRADE_FIELDS          # noqa: E402

CFG = day_desk.load_config(ROOT)
WL = ["AMD"]


def ts(h, m, day=28):
    return datetime(2026, 10, day, h, m, tzinfo=ET).timestamp()


def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def trade(entry_t, raw=100.0, stop=99.0, target=102.0, shares=10, exit_px=None, exit_t=None, why_out="x"):
    exit_px = raw if exit_px is None else exit_px
    entry = round(raw * (1 + COST_PER_SIDE), 4)
    ex = round(exit_px * (1 - COST_PER_SIDE), 4)
    return {"sym": "AMD", "shares": shares, "entry": entry, "exit": ex, "entry_t": entry_t,
            "exit_t": exit_t or entry_t + 600, "pnl": round((ex - entry) * shares, 2),
            "pnl_pct": round((ex / entry - 1) * 100, 3), "why_in": "t", "why_out": why_out,
            "setup": "test", "stop": stop, "target": target, "raw_entry": raw}


def random_walk(seed, n=60, start=(11, 0)):
    import random
    rnd = random.Random(seed)
    out, p, t0 = [], 100.0, ts(*start)
    for i in range(n):
        o = p
        c = o * (1 + rnd.uniform(-0.004, 0.004))
        h = max(o, c) * (1 + rnd.uniform(0, 0.003))
        l = min(o, c) * (1 - rnd.uniform(0, 0.003))
        if rnd.random() < 0.1:                       # occasional gap
            o = o * (1 + rnd.choice([-1, 1]) * 0.006)
            h, l = max(h, o), min(l, o)
        out.append(bar(t0 + 60 * (i + 1), o, h, l, c))
        p = c
    return out


# ---------------- fill-rule parity with the engine ----------------
@pytest.mark.parametrize("seed", range(25))
def test_walk_matches_book_check_exits(tmp_path, seed):
    bars = random_walk(seed)
    b = Book(str(tmp_path), CFG)
    t0 = ts(11, 0) + 5
    assert b.buy("AMD", 100.0, 10, 99.4, 100.6, "t", t0, WL) is None
    real = b.check_exits("AMD", bars)
    mine = lab.walk_exits(bars, 99.4, 100.6, t0)
    if real is None:
        assert mine is None
    else:
        assert mine is not None
        assert real["exit"] == pytest.approx(round(mine["price"] * (1 - COST_PER_SIDE), 4))
        assert real["exit_t"] == mine["t"]
        assert real["why_out"].split()[0] == mine["reason"]


def test_stop_whatif_x1_equals_engine_when_it_exits(tmp_path):
    bars = [bar(ts(11, 1), 100, 100.2, 99.9, 100.1), bar(ts(11, 2), 100.1, 100.1, 98.5, 98.7)]
    b = Book(str(tmp_path), CFG)
    assert b.buy("AMD", 100.0, 10, 99.0, 102.0, "t", ts(11, 0) + 5, WL) is None
    real = b.check_exits("AMD", bars)
    w = lab.stop_whatif(trade(ts(11, 0) + 5), bars, 1.0, CFG)
    assert w["exit_reason"] == "stop" and w["pnl"] == pytest.approx(real["pnl"], abs=0.01)


def test_gap_through_stop_fills_at_open():
    bars = [bar(ts(11, 1), 98.0, 98.2, 97.5, 98.1)]
    w = lab.stop_whatif(trade(ts(11, 0)), bars, 1.0, CFG)
    assert w["exit_reason"] == "stop" and w["exit_price"] == 98.0


def test_gap_through_target_fills_at_open():
    bars = [bar(ts(11, 1), 103.0, 103.5, 102.8, 103.2)]
    w = lab.stop_whatif(trade(ts(11, 0)), bars, 1.0, CFG)
    assert w["exit_reason"] == "target" and w["exit_price"] == 103.0


def test_both_in_one_bar_is_stop():
    bars = [bar(ts(11, 1), 100.0, 102.5, 98.5, 100.0)]
    w = lab.stop_whatif(trade(ts(11, 0)), bars, 1.0, CFG)
    assert w["exit_reason"] == "stop" and w["exit_price"] == 99.0


def test_no_hit_sells_at_close_out_and_ignores_later_bars():
    flat = CFG["flatten_minute"]
    t_last = ts(flat // 60, flat % 60) - 60
    bars = [bar(ts(11, 1), 100, 100.5, 99.5, 100.3), bar(t_last, 100.4, 100.6, 100.2, 100.5),
            bar(t_last + 60, 100.5, 105, 90, 95)]          # after 3:45: must be ignored
    w = lab.stop_whatif(trade(ts(11, 0)), bars, 1.0, CFG)
    assert w["exit_reason"] == "close-out" and w["exit_price"] == 100.5


def test_no_target_means_stop_or_close_out_only():
    bars = [bar(ts(11, 1), 100, 110, 99.5, 109)]
    w = lab.stop_whatif(trade(ts(11, 0), target=None), bars, 1.0, CFG)
    assert w["exit_reason"] == "close-out"


def test_whatif_stop_clamped_at_hard_cap():
    cap = CFG["max_stop_pct"]
    w = lab.stop_whatif(trade(ts(11, 0), stop=98.0), [bar(ts(11, 1), 100, 100, 90, 91)], 2.0, CFG)
    assert w["clamped"] and "clamped" in w["note"]
    assert w["stop"] == pytest.approx(100 * (1 - cap / 100))
    w2 = lab.stop_whatif(trade(ts(11, 0), stop=99.0), [bar(ts(11, 1), 100, 100, 90, 91)], 2.0, CFG)
    assert not w2["clamped"] and w2["stop"] == pytest.approx(98.0)


# ---------------- excursions ----------------
def test_excursions_hand_built():
    e = ts(10, 40) + 10                    # bought at 10:40:10
    x = ts(10, 45)                         # stopped on the 10:45 bar
    morning = [bar(ts(9, 30) + 60 * i, 100, 100.2, 99.8, 100) for i in range(70)]   # 9:30-10:39, range 0.4%
    trade_bars = [bar(ts(10, 41), 100, 101.0, 99.7, 100.5),     # MFE +1.0%
                  bar(ts(10, 42), 100.5, 100.6, 99.5, 99.6),
                  bar(ts(10, 43), 99.6, 99.8, 99.3, 99.4),
                  bar(ts(10, 44), 99.4, 99.5, 99.2, 99.3),
                  bar(ts(10, 45), 99.3, 99.3, 98.9, 99.0)]     # stop 99.0 hit, low 98.9 -> MAE -1.1%
    after = [bar(ts(10, 46), 99.0, 101.5, 98.5, 101.0),         # rises past the 102 target later
             bar(ts(11, 0), 101.0, 102.3, 100.9, 101.8),
             bar(ts(15, 44), 101.0, 101.2, 100.8, 100.7),       # 3:44 bar close = flatten close
             bar(ts(15, 50), 100.7, 110, 80, 100)]              # after flatten: ignored
    tr = trade(e, stop=99.0, target=102.0, exit_px=99.0, exit_t=x, why_out="stop hit 10:45 AM")
    r = lab.excursions(tr, morning + trade_bars + after, CFG)
    assert r["mfe_pct"] == pytest.approx(1.0)
    assert r["mae_pct"] == pytest.approx(-1.1)
    assert r["held_min"] == pytest.approx(4.8, abs=0.05)
    assert r["exit_reason"] == "stop"
    assert r["after_max_high_pct"] == pytest.approx(2.3)
    assert r["after_min_low_pct"] == pytest.approx(-1.5)
    assert r["close_at_flatten_pct"] == pytest.approx(0.7)
    assert r["stopped_then_recovered"] is True
    assert r["stop_pct"] == pytest.approx(1.0) and r["target_pct"] == pytest.approx(2.0)
    assert r["typical_1m_range_pct"] == pytest.approx(0.4)
    assert r["stop_in_typical_ranges"] == pytest.approx(2.5)
    assert r["first_hour"] is False


# ---------------- report helpers on a temp folder ----------------
def make_folder(tmp_path, trades_rows, bars_by_day):
    shutil.copy(os.path.join(ROOT, "config.json"), tmp_path / "config.json")
    (tmp_path / "data" / "bars").mkdir(parents=True)
    with open(tmp_path / "data" / "trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for t in trades_rows:
            w.writerow({k: t[k] for k in TRADE_FIELDS})
    with open(tmp_path / "data" / "decisions.jsonl", "w", encoding="utf-8") as f:
        for t in trades_rows:
            f.write(json.dumps({"t": t["entry_t"] + 0.3, "sym": t["sym"], "action": "buy", "price": t["raw_entry"],
                                "stop": t["stop"], "target": t["target"], "shares": t["shares"]}) + "\n")
    for day, bars in bars_by_day.items():
        with open(tmp_path / "data" / "bars" / f"{day}.json", "w", encoding="utf-8") as f:
            json.dump({"day": day, "bars": bars}, f)
    return str(tmp_path)


def many_trades_folder(tmp_path, k=30):
    bars = [bar(ts(9, 30) + 60 * i, 100, 100.3, 99.7, 100) for i in range(375)]
    rows = []
    for i in range(k):
        e = ts(9, 50) + 600 * i + 7
        why = "stop hit" if i % 2 else "Claude sold"
        rows.append(trade(e, exit_t=e + 300, exit_px=99.5 if i % 2 else 100.2, why_out=why))
    return make_folder(tmp_path, rows, {"2026-10-28": {"AMD": bars}})


def test_review_facts_caps_and_hindsight_line(tmp_path):
    folder = many_trades_folder(tmp_path)
    txt = lab.review_facts(folder, "2026-10-28")
    lines = txt.split("\n")
    assert len(lines) <= 25 and len(txt) <= 2500
    assert lines[-1].startswith("Hindsight on 30 trades")
    assert "more trades not shown" in txt
    assert "Across all 30 trades" in txt


def test_review_facts_empty_day(tmp_path):
    folder = many_trades_folder(tmp_path, k=3)
    assert lab.review_facts(folder, "2026-10-27") == ""


def test_report_is_deterministic_and_honest(tmp_path):
    folder = many_trades_folder(tmp_path, k=8)
    a, b = lab.lab_report(folder), lab.lab_report(folder)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    txt = lab.report_text(a)
    assert txt == lab.report_text(b)
    assert txt.count("HINDSIGHT on n=") >= 3 and "curve-fitting" in txt and "not a rule" in txt
    assert "Noise check" in txt
    assert a["reentry_after_stop"]["n"] >= 1


def test_cli_writes_both_files(tmp_path, capsys):
    folder = many_trades_folder(tmp_path, k=4)
    lab.main(folder)
    assert os.path.getsize(os.path.join(folder, "data", "lab_report.txt")) > 0
    json.load(open(os.path.join(folder, "data", "lab_report.json"), encoding="utf-8"))
    assert "MFE" in capsys.readouterr().out


def test_symbol_today(tmp_path):
    folder = many_trades_folder(tmp_path, k=2)
    now = ts(15, 0)
    line = lab.symbol_today(folder, "AMD", "2026-10-28", now, last_price=100.5)
    assert line.startswith("Earlier today: ") and "stopped out of AMD" in line and "price since then" in line
    assert lab.symbol_today(folder, "NVDA", "2026-10-28", now) == ""
    assert lab.symbol_today(folder, "AMD", "2026-10-28", ts(9, 0)) == ""     # nothing closed yet
