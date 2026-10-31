"""Tests for decision_score.py (hindsight score of every buy AND pass). Run TEST.bat (or: python -m pytest -q tests)."""
import json
import os
import shutil
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brain                                                      # noqa: E402
import day_desk                                                   # noqa: E402
import decision_score as ds                                       # noqa: E402
from engine import ET, COST_PER_SIDE                              # noqa: E402

CFG = day_desk.load_config(ROOT)


def ts(h, m, day=28):
    return datetime(2026, 10, day, h, m, tzinfo=ET).timestamp()


def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def morning(day=28, n=30, price=100.0, rng=0.5):
    """9:30 onward, each bar range = rng (0.5% of 100) -> typical 0.5%, neutral stop 1% below entry, target +1.5%."""
    t0 = ts(9, 30, day)
    return [bar(t0 + 60 * i, price, price + rng / 2, price - rng / 2, price) for i in range(n)]


# ---------- row selection ----------
def test_row_selection_exclusions_and_bought_only_buys():
    rows = [
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "buy", "result": "bought 4 @ 100.01"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "buy", "result": "refused: stop too far"},
        {"t": 1, "sym": "AMD", "trigger": "flush_bounce", "action": "pass", "result": "no trade"},
        {"t": 1, "sym": "AMD", "trigger": "vwap_reclaim", "action": "pass", "result": "holding"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "adjust", "result": "stop/target moved"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "-", "result": "skipped: daily cap"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "error", "result": "Claude call failed"},
        {"t": 1, "sym": "AMD", "trigger": "review", "action": "pass", "result": "no trade"},
        {"t": 1, "sym": "AMD", "trigger": "resting order", "action": "sell", "result": "x"},
        {"t": 1, "sym": "AMD", "trigger": "3:45 PM close-out", "action": "sell", "result": "x"},
        {"t": 1, "sym": "-", "trigger": "self-test", "action": "self-test"},
        {"t": 1, "sym": "-", "trigger": "morning research", "action": "research"},
        {"t": 1, "sym": "-", "trigger": "end-of-day review", "action": "review"},
        {"t": 1, "sym": "TSLA", "trigger": "breakout", "action": "pass", "result": "no trade"},
    ]
    got = ds.judgments(rows)
    assert [(r["trigger"], r["action"]) for r in got] == [("momentum", "buy"), ("flush_bounce", "pass"),
                                                          ("breakout", "pass")]
    assert set(ds.NOT_CLAUDE_CALLS) == set(day_desk.NOT_CLAUDE_CALLS) <= ds.NON_ENTRY_TRIGGERS


# ---------- the neutral bracket ----------
def test_neutral_bracket_target_first():
    bars = morning()
    t = bars[-1]["t"] + 30                                     # decision mid-bar
    bars += [bar(t + 30, 100.0, 100.5, 99.8, 100.4), bar(t + 90, 100.4, 101.6, 100.3, 101.5)]
    r = ds.neutral_bracket(bars, t, CFG)
    assert r["entry"] == 100.0 and r["entry_t"] == t + 30       # OPEN of the first bar after the decision
    assert r["typical_pct"] == pytest.approx(0.5)
    assert r["stop"] == pytest.approx(99.0) and r["target"] == pytest.approx(101.5)
    assert r["outcome"] == "target" and r["exit"] == pytest.approx(101.5)
    net = 101.5 * (1 - COST_PER_SIDE) - 100 * (1 + COST_PER_SIDE)
    assert r["r"] == pytest.approx(net / 1.0, abs=1e-4)        # R net of cost


def test_neutral_bracket_stop_first_and_both_in_one_bar_is_stop():
    bars = morning()
    t = bars[-1]["t"] + 59
    bars += [bar(t + 1, 100.0, 101.7, 98.9, 100.0)]            # touches both -> stop
    r = ds.neutral_bracket(bars, t, CFG)
    assert r["outcome"] == "stop" and r["exit"] == pytest.approx(99.0) and r["r"] < -1


def test_neutral_bracket_gap_through_stop_fills_at_open():
    bars = morning()
    t = bars[-1]["t"] + 59
    bars += [bar(t + 1, 100.0, 100.1, 99.9, 100.0), bar(t + 61, 98.0, 98.2, 97.8, 98.0)]
    r = ds.neutral_bracket(bars, t, CFG)
    assert r["outcome"] == "stop" and r["exit"] == pytest.approx(98.0) and r["r"] < -1.9


def test_neutral_bracket_flatten_and_ignores_bars_after_flatten():
    bars = morning()
    t0 = ts(15, 40)
    bars += [bar(t0 + 60 * i, 100.0, 100.1, 99.9, 100.2) for i in range(5)]   # 3:40 .. 3:44
    bars += [bar(ts(15, 45), 100.0, 105.0, 99.95, 104.0)]                      # 3:45: after flatten, ignored
    r = ds.neutral_bracket(bars, t0 - 1, CFG)
    assert r["outcome"] == "flatten" and r["exit"] == pytest.approx(100.2)
    assert ds.neutral_bracket(bars, ts(15, 45), CFG) is None                  # nothing left before 3:45


def test_neutral_stop_clamped_at_max_stop_pct():
    bars = morning(rng=4.0)                                    # typical 4% -> 8% stop, capped at 3%
    t = bars[-1]["t"] + 59
    bars += [bar(t + 1, 100.0, 100.1, 99.9, 100.0)]
    r = ds.neutral_bracket(bars, t, CFG)
    assert r["clamped"] and r["stop"] == pytest.approx(100 * (1 - CFG["max_stop_pct"] / 100))


def test_fwd_vs_qqq():
    bars = morning(n=5)
    qqq = morning(n=5, price=500.0)
    t = bars[0]["t"] - 1
    bars += [bar(bars[-1]["t"] + 60 * (i + 1), 100, 101, 99, 101.0) for i in range(30)]
    qqq += [bar(qqq[-1]["t"] + 60 * (i + 1), 500, 501, 499, 500.0) for i in range(30)]
    assert ds.fwd_vs_qqq(bars, qqq, t, 30) == pytest.approx(1.0)
    assert ds.fwd_vs_qqq(bars, qqq, t, 60) is None              # bars do not reach 60 minutes


# ---------- bootstrap ----------
def _rows(days, buy_r, pass_r):
    out = []
    for i, d in enumerate(days):
        out.append({"day": d, "action": "buy", "r": buy_r + 0.1 * i, "outcome": "target", "trigger": "momentum"})
        out.append({"day": d, "action": "pass", "r": pass_r - 0.1 * i, "outcome": "stop", "trigger": "momentum"})
        out.append({"day": d, "action": "pass", "r": pass_r, "outcome": "stop", "trigger": "flush_bounce"})
    return out


def test_day_bootstrap_is_deterministic_and_resamples_whole_days():
    rows = _rows([f"2026-11-0{i}" for i in range(2, 9)], 1.0, -1.0)
    a, b = ds.day_bootstrap(rows), ds.day_bootstrap(rows)
    assert a == b and a["resamples_used"] == ds.BOOT_N and a["n_days"] == 7
    assert a["ci90"][0] > 0 and a["verdict"].startswith("buys ahead")
    assert ds.day_bootstrap(rows, seed=1)["resamples_used"] == ds.BOOT_N         # seed is the only randomness
    few = ds.day_bootstrap(_rows(["2026-11-02", "2026-11-03", "2026-11-04"], 1.0, -1.0))
    assert few["verdict"].startswith("too few days")           # never call a side on < MIN_BOOT_DAYS
    assert ds.day_bootstrap([r for r in rows if r["action"] == "pass"])["diff"] is None


def test_within_noise_when_ci_spans_zero():
    rows = []
    for i, d in enumerate([f"2026-11-{k:02d}" for k in range(2, 12)]):
        s = 1 if i % 2 else -1
        rows += [{"day": d, "action": "buy", "r": s * 1.0, "outcome": "x", "trigger": "m"},
                 {"day": d, "action": "pass", "r": -s * 1.0, "outcome": "x", "trigger": "m"}]
    assert ds.day_bootstrap(rows)["verdict"] == "within noise"


# ---------- calibration ----------
def test_calibration_waits_for_ten_rows_then_bins_and_brier():
    rows = [{"p_win": 70, "outcome": "target"} for _ in range(5)] + [{"p_win": 30, "outcome": "stop"} for _ in range(4)]
    assert ds.calibration(rows)["ready"] is False and "not enough yet" in ds.calibration(rows)["note"]
    rows.append({"p_win": 100, "outcome": "flatten"})
    rows.append({"p_win": None, "outcome": "target"})
    c = ds.calibration(rows)
    assert c["ready"] and c["n"] == 10
    b = {x["bin"]: x for x in c["bins"]}
    assert b["0-40"]["n"] == 4 and b["0-40"]["target_first_pct"] == 0 and b["0-40"]["brier"] == pytest.approx(0.09)
    assert b["40-60"]["n"] == 0 and b["40-60"]["brier"] is None
    assert b["60-100"]["n"] == 6 and b["60-100"]["mean_p_win"] == pytest.approx(75)       # 100 lands in the top bin
    assert b["60-100"]["brier"] == pytest.approx((5 * 0.09 + 1.0) / 6, abs=1e-4)


# ---------- brain: p_win ----------
def test_parse_decision_p_win_tolerance():
    p = lambda v: brain.parse_decision('{"action":"pass","why":"x","p_win":%s}' % v)["p_win"]
    assert p("65") == 65 and p('"65%"') == 65 and p('"55"') == 55 and p("62.6") == 63
    assert p("150") == 100 and p("-5") == 0
    assert p('"N/A"') is None and p("null") is None and p('"abc"') is None and p("true") is None
    assert brain.parse_decision('{"action":"pass","why":"x"}')["p_win"] is None


def test_system_prompt_asks_for_p_win():
    assert '"p_win"' in brain.SYSTEM and "before its stop" in brain.SYSTEM and "for a pass" in brain.SYSTEM


def test_ask_stores_p_win(tmp_path):
    from test_day_desk import make_desk, breakout_set
    bars = breakout_set()
    d, _ = make_desk(tmp_path, bars, [{"action": "pass", "why": "meh", "p_win": "40%"}])
    d.step(bars["AMD"][-1]["t"] + 65)
    assert d.recent[-1]["action"] == "pass" and d.recent[-1]["p_win"] == 40


# ---------- review_lines + integration ----------
def _folder(tmp_path, n_days=1):
    data = tmp_path / "data"
    (data / "bars").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "config.json"), tmp_path / "config.json")
    recs = []
    for k in range(n_days):
        day = 26 + k
        amd = morning(day=day, n=60)
        amd += [bar(amd[-1]["t"] + 60 * (i + 1), 100.0, 101.6, 99.9, 101.5) for i in range(20)]
        qqq = morning(day=day, n=80, price=500.0)
        with open(data / "bars" / f"2026-10-{day}.json", "w") as f:
            json.dump({"day": f"2026-10-{day}", "bars": {"AMD": amd, "QQQ": qqq}}, f)
        for i in range(12):
            act = "buy" if i % 4 == 0 else "pass"
            recs.append({"t": ts(10, 0 + i * 3, day), "sym": "AMD", "trigger": "momentum" if i % 2 else "flush_bounce",
                         "action": act, "p_win": 60, "why": "x" * 300,
                         "result": "bought 4 @ 100.00" if act == "buy" else "no trade"})
    with open(data / "decisions.jsonl", "w") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(tmp_path)


def test_review_lines_caps_and_tail(tmp_path):
    folder = _folder(tmp_path, n_days=2)
    txt = ds.review_lines(folder, "2026-10-27")
    assert txt.endswith("Hindsight — a clue, not proof.")
    assert len(txt.splitlines()) <= 5 and len(txt) <= 700
    assert "Diagnostic only" in txt and "n=" in txt and "Brier" in txt
    assert ds.review_lines(str(tmp_path / "nothing"), "2026-10-27") == ""


def test_report_and_cli_files(tmp_path):
    folder = _folder(tmp_path)
    rep, txt = ds.write_report(folder)
    assert rep["n"] == 12 and rep["header"].startswith("Hindsight on 12 judgments over 1 days")
    assert "Diagnostic only — not the grade." in txt and txt.rstrip().endswith("a clue, not proof.")
    j = json.load(open(os.path.join(folder, "data", "decision_score.json"), encoding="utf-8"))
    assert j["n"] == 12 and set(j["by_action"]) == {"buy", "pass"}
    assert os.path.exists(os.path.join(folder, "data", "decision_score.txt"))
    assert rep["calibration"]["ready"] and "momentum" in rep["by_trigger"]
    bat = open(os.path.join(ROOT, "DECISION_SCORE.bat"), "rb").read()
    assert b"decision_score.py" in bat and b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")


def test_lab_facts_never_raises_if_decision_score_errors(tmp_path, monkeypatch):
    from test_day_desk import make_desk, breakout_set
    d, _ = make_desk(tmp_path, breakout_set(), [])
    monkeypatch.setattr(ds, "review_lines", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert d.lab_facts("2026-10-28") == ""
    monkeypatch.setattr(ds, "review_lines", lambda *a, **k: "DS LINE\nHindsight — a clue, not proof.")
    assert d.lab_facts("2026-10-28").startswith("DS LINE")
