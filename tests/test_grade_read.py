"""Tests for grade_read.py, the pre-registered grade. Synthetic data only."""
import json
import os
import random
import statistics
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grade_read as gr                                          # noqa: E402
from engine import Book, ET, COST_PER_SIDE                       # noqa: E402
import day_desk                                                  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = day_desk.load_config(HERE)


def make(tmp_path, n, mode="random", seed=1, drop=()):
    f = str(tmp_path / f"f{n}_{mode}_{seed}_{len(drop)}")
    gr._synth(f, n, gr.SYN_DAYS, seed, mode, drop_days=drop)
    return f


# ---------------- the no-verdict guard
@pytest.mark.parametrize("n", [1, 10, 59])
def test_below_60_prints_no_verdict(tmp_path, n, capsys):
    f = make(tmp_path, n, "best")
    text = gr.main(f)
    out = capsys.readouterr().out
    for t in (text, out, open(os.path.join(f, "data", "grade_report.txt")).read()):
        assert "PROGRESS ONLY" in t
        for w in ("PASS", "FAIL", "PERCENTILE", "VERDICT", "G1", "G2", "BEAT"):
            assert w not in t.upper()
    prog = json.load(open(os.path.join(f, "data", "grade_progress.json")))
    assert prog["closed"] == n and prog["needed"] == 60 and prog["read"] == "Read 1"
    assert prog["research_needed"] == 40 and prog["bars_coverage"] == n and prog["stops_known"] == n


def test_read1_grades_first_60_by_exit_and_notes_later(tmp_path):
    f = make(tmp_path, 70, "best")
    text, prog = gr.build_report(f, n_copies=200)
    assert "Read 1: first 60 closed trades" in text and "10 later trade(s)" in text
    assert "of 2 gates met" in text and "Resolution floor" in text and "SPY not archived" in text
    assert "Read 2" not in text.split("Next:")[0]
    assert prog["read"] == "Read 2" and prog["needed"] == 120


def test_read2_labelled(tmp_path):
    f = make(tmp_path, 120, "random")
    text, _ = gr.build_report(f, n_copies=50)
    assert "Read 2: first 120 closed trades" in text and "Read 1: first 60" in text


# ---------------- window ruling
def test_entry_window_ruling():
    assert gr.entry_window(1) == (585, 930)          # capped at 15:30
    assert gr.entry_window(15) == (585, 930)
    assert gr.entry_window(16) == (585, 929)         # 15:45 - 16 min
    assert gr.entry_window(120) == (585, 825)
    assert gr.entry_window(360) == (585, 585)
    assert gr.entry_window(500) == (585, 585)        # empty window -> 9:45 only
    for h in (1, 30, 200, 360):
        lo, hi = gr.entry_window(h)
        assert hi + h <= 945 or hi == 585


# ---------------- excluded days
def test_one_day_missing_is_10pct_still_graded(tmp_path):
    f = make(tmp_path, 60, "random", 3, drop=(gr.SYN_DAYS[0],))
    g = gr.grade(gr.load_trades(f), gr.Bars(f), n=100)
    assert g["n_left_out"] == 6 and g["g2"] is not None


def test_over_10pct_missing_is_unknown(tmp_path):
    f = make(tmp_path, 60, "best", 4, drop=tuple(gr.SYN_DAYS[:2]))
    tr = gr.load_trades(f)
    g = gr.grade(tr, gr.Bars(f), n=100)
    assert g["n_left_out"] == 12 and g["g2"] is None and g["verdict"] == "UNKNOWN"
    assert g["total"] == round(sum(t["pnl"] for t in tr), 2)       # left-out trades still count in G1
    text, _ = gr.build_report(f, n_copies=100)
    assert "UNKNOWN" in text and "VERDICT: PASS" not in text


# ---------------- stop-applied copies match engine.check_exits
def ts(h, m):
    return datetime(2026, 10, 28, h, m, tzinfo=ET).timestamp()


@pytest.mark.parametrize("seq", [
    [(100.2, 100.4, 99.9, 100.1), (99.5, 99.6, 98.9, 99.0), (99.0, 99.2, 98.5, 98.6)],   # touches stop
    [(100.0, 100.1, 99.7, 99.8), (97.5, 97.8, 97.0, 97.6), (97.6, 97.9, 97.2, 97.5)],    # gaps through
    [(100.0, 100.3, 99.6, 100.2), (100.2, 100.5, 99.8, 100.4), (100.4, 100.6, 100.1, 100.5)],  # no stop
])
def test_stop_copy_matches_engine(tmp_path, seq):
    entry_raw, dist, shares = 100.0, 1.0, 10
    bm = {600: {"t": ts(10, 0), "o": 100, "h": 100, "l": 100, "c": entry_raw}}
    eng_bars = []
    for i, (o, h, l, c) in enumerate(seq, start=1):
        b = {"t": ts(10, i), "o": o, "h": h, "l": l, "c": c, "v": 1}
        bm[600 + i] = b
        eng_bars.append(b)
    mine = gr.copy_pnl(bm, 600, len(seq), shares, stop_dist=dist)
    book = Book(str(tmp_path), CFG)
    assert book.buy("AMD", entry_raw, shares, entry_raw - dist, 1e9, "x", ts(10, 0) + 30, ["AMD"]) is None
    tr = book.check_exits("AMD", eng_bars)
    if tr is None:                                         # time exit at the last bar's close
        tr = book.sell("AMD", seq[-1][3], ts(10, len(seq)), "time")
    assert mine == pytest.approx(tr["pnl"], abs=0.01)


# ---------------- floor, stress, drawdown, determinism, selftest
def test_floor_definition():
    vals = list(range(1, 2001))
    assert gr.p95(vals) == 1900
    assert gr.floor_of(vals) == pytest.approx(1900 - statistics.median(vals))


def test_stress_and_drawdown():
    t = {"shares": 10, "entry": 100 * (1 + COST_PER_SIDE), "exit": 101 * (1 - COST_PER_SIDE), "exit_t": 1, "pnl": 9.98}
    assert gr.stress_pnl(t) == pytest.approx(10 * (101 * 0.9995 - 100 * 1.0005))
    tr = [{"exit_t": i, "pnl": p} for i, p in enumerate([5, -3, -4, 10, -12, 1])]
    assert gr.max_drawdown(tr) == pytest.approx(12)


def test_deterministic(tmp_path):
    f = make(tmp_path, 60, "random", 7)
    a = gr.build_report(f, n_copies=150)[0].split("\n", 1)[1]
    b = gr.build_report(f, n_copies=150)[0].split("\n", 1)[1]
    assert a == b


def test_selftest_passes():
    assert gr.selftest().startswith("SELFTEST OK")


# ---------------- research grade
def test_research_progress_and_read(tmp_path):
    f = str(tmp_path)
    os.makedirs(os.path.join(f, "data"))
    assert gr.research(f)["graded_high"] == 0
    rng = random.Random(3)
    with open(os.path.join(f, "data", "pick_results.jsonl"), "w") as fh:
        for i in range(45):
            day = f"2026-11-{(i % 20) + 1:02d}"
            fh.write(json.dumps({"day": day, "ticker": "X", "conviction": "high", "vs_qqq_pct": 1 + rng.random()}) + "\n")
            fh.write(json.dumps({"day": day, "ticker": "Y", "conviction": "low", "vs_qqq_pct": rng.random() - 0.5}) + "\n")
        fh.write(json.dumps({"day": "2026-11-01", "ticker": "Z", "conviction": "high", "vs_qqq_pct": None}) + "\n")
    r = gr.research(f)
    assert r["graded_high"] == 45 and r["ready"] and r["lo"] > 0 and r["edge"]
    assert gr.research(f)["lo"] == r["lo"]                 # seeded bootstrap


def test_grader_and_desk_agree_on_what_is_not_a_claude_call():
    import day_desk
    import grade_read
    assert grade_read.NOT_CLAUDE_CALLS == day_desk.NOT_CLAUDE_CALLS
