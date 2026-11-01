"""Tests for the 10-31 evening build: pre-reg Addendum A4 control arm (decision_score.py + grade_read.py),
the prompt "facts" audit (brain.build_prompt, Desk.ask) and friction-in-R (lab.py). Synthetic data only."""
import json
import os
import shutil
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain                                                      # noqa: E402
import decision_score as ds                                       # noqa: E402
import grade_read as gr                                           # noqa: E402
import lab                                                        # noqa: E402
from engine import ET, COST_PER_SIDE                              # noqa: E402

FORBIDDEN = ("PASS", "FAIL", "PERCENTILE", "VERDICT", "G1", "G2", "BEAT")     # test_grade_read's no-verdict list


def ts(h, m, day=2, month=11):
    return datetime(2026, month, day, h, m, tzinfo=ET).timestamp()


def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat_day(day, price=100.0, n=60, rng=0.5, drift=0.0, v=1000):
    t0 = ts(9, 30, day)
    out = []
    for i in range(n):
        p = price + drift * i
        out.append(bar(t0 + 60 * i, p, p + rng / 2, p - rng / 2, p, v))
    return out


# ---------------- candidate selection (rulings in decision_score's A4 block)
def test_a4_candidate_rules():
    rows = [
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "buy", "result": "bought 4 @ 100.01"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "buy", "result": "refused: stop too far"},
        {"t": 1, "sym": "AMD", "trigger": "flush_bounce", "action": "pass", "result": "no trade"},
        {"t": 1, "sym": "AMD", "trigger": "vwap_reclaim", "action": "pass", "result": "holding"},
        {"t": 1, "sym": "AMD", "trigger": "breakout", "action": "pass", "result": "no trade", "held": True},
        {"t": 1, "sym": "AMD", "trigger": "breakout", "action": "buy", "result": "ignored: 'buy' doesn't fit (held)"},
        {"t": 1, "sym": "AMD", "trigger": "review", "action": "pass", "result": "no trade"},
        {"t": 1, "sym": "AMD", "trigger": "protect-gain", "action": "pass", "result": "holding"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "-", "result": "skipped: daily cap"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "error", "result": "Claude call failed"},
        {"t": 1, "sym": "AMD", "trigger": "momentum", "action": "hold", "result": "holding"},
        {"t": 1, "sym": "-", "trigger": "momentum", "action": "pass", "result": "no trade"},
        {"sym": "AMD", "trigger": "momentum", "action": "pass", "result": "no trade"},
    ]
    got = [(r["trigger"], r["action"], r["result"][:7]) for r in rows if ds.is_a4_candidate(r)]
    assert got == [("momentum", "buy", "bought "), ("momentum", "buy", "refused"), ("flush_bounce", "pass", "no trad")]
    assert ds.A4_TRIGGERS == ("momentum", "flush_bounce", "vwap_reclaim", "breakout")
    assert ds.A4_START_DAY == "2026-11-02"


# ---------------- the GATED rule, exactly as written
def test_gate_takes_only_above_vwap_and_stronger_than_qqq():
    up = flat_day(2, drift=0.05)                    # rising stock: close > VWAP, +% since open
    qqq_flat = flat_day(2, price=500.0)
    qqq_strong = flat_day(2, price=500.0, drift=1.0)
    t = ts(10, 0)
    g, d = ds.a4_gate(up, qqq_flat, t)
    assert g is True and d["above_vwap"] and d["beats_qqq"]
    g, d = ds.a4_gate(up, qqq_strong, t)            # stock weaker than the Nasdaq -> not taken
    assert g is False and d["above_vwap"] and not d["beats_qqq"]
    down = flat_day(2, drift=-0.05)                 # falling: below VWAP
    g, d = ds.a4_gate(down, qqq_flat, t)
    assert g is False and not d["above_vwap"]


def test_gate_uses_finished_bars_only_and_same_bars_for_qqq():
    st = flat_day(2, drift=0.05)
    t = ts(10, 0)
    forming = [b for b in st if b["t"] < t] + [bar(t - 30, 200.0, 200.0, 50.0, 50.0, 10 ** 9)]   # still forming at t
    g1, d1 = ds.a4_gate(st, flat_day(2, price=500.0), t)
    g2, d2 = ds.a4_gate(forming, flat_day(2, price=500.0), t)
    assert d1["close"] == d2["close"] and g1 == g2                 # the forming bar is ignored
    # QQQ only up to the stock's last finished bar: a stock missing its last 5 minutes compares with QQQ at that time
    short = [b for b in st if b["t"] < ts(9, 55)]
    q = flat_day(2, price=500.0)
    q = [dict(b, c=500.0 + (100 if b["t"] >= ts(9, 55) else 0)) for b in q]   # QQQ jumps only after 9:55
    g, d = ds.a4_gate(short, q, t)
    assert d["qqq_pct"] == 0.0 and g is True


def test_gate_unknown_never_says_yes():
    t = ts(10, 0)
    assert ds.a4_gate([], flat_day(2, price=500.0), t)[0] is None
    assert ds.a4_gate(flat_day(2, drift=0.05), [], t)[0] is None
    assert ds.a4_gate(flat_day(2, drift=0.05, v=0), flat_day(2, price=500.0), t)[0] is None   # no volume -> no VWAP
    premarket = [bar(ts(9, 0) + 60 * i, 100, 101, 99, 100) for i in range(10)]
    assert ds.a4_gate(premarket, flat_day(2, price=500.0), t)[0] is None                    # nothing from 9:30


# ---------------- whole report on a synthetic folder
def _folder(tmp_path, days=(2, 3, 4, 5, 6), start_before=True):
    data = tmp_path / "data"
    (data / "bars").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "config.json"), tmp_path / "config.json")
    recs = []
    for day in days:
        up = flat_day(day, n=120, drift=0.05)          # trends up all morning: target first for any entry
        dn = flat_day(day, price=50.0, n=120, drift=-0.02)
        qqq = flat_day(day, price=500.0, n=120)
        with open(data / "bars" / f"2026-11-{day:02d}.json", "w") as f:
            json.dump({"day": f"2026-11-{day:02d}", "bars": {"UP": up, "DN": dn, "QQQ": qqq}}, f)
        recs += [
            {"t": ts(10, 0, day), "sym": "UP", "trigger": "momentum", "action": "buy", "result": "bought 4 @ 101"},
            {"t": ts(10, 5, day), "sym": "DN", "trigger": "flush_bounce", "action": "buy", "result": "refused: x"},
            {"t": ts(10, 10, day), "sym": "UP", "trigger": "breakout", "action": "pass", "result": "no trade"},
            {"t": ts(10, 15, day), "sym": "DN", "trigger": "vwap_reclaim", "action": "pass", "result": "no trade"},
            {"t": ts(10, 20, day), "sym": "NOBARS", "trigger": "momentum", "action": "pass", "result": "no trade"},
        ]
    if start_before:                                   # a candidate BEFORE 11-02 must never be counted
        recs.append({"t": ts(10, 0, 30, 10), "sym": "UP", "trigger": "momentum", "action": "buy", "result": "bought 1"})
    with open(data / "decisions.jsonl", "w") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(tmp_path)


def test_control_arms_numbers(tmp_path):
    f = _folder(tmp_path)
    ca = ds.control_arms(f)
    assert ca["n_candidates"] == 25 and ca["n_scored"] == 20 and ca["n_dropped"] == 5     # 10-30 row not counted
    assert ca["gate_unknown"] == 0
    A = ca["arms"]
    assert A["ALL"]["n"] == 20 and A["GATED"]["n"] == 10 and A["CLAUDE"]["n"] == 10
    o = ca["overlap"]
    assert (o["both"], o["claude_only"], o["gated_only"], o["neither"]) == (5, 5, 5, 5) and o["agree_pct"] == 50.0
    # GATED took exactly the UP rows (above VWAP and stronger than flat QQQ); UP rows hit the target first
    rows, _ = ds.a4_candidates(f)
    assert all(r["gated"] == (r["sym"] == "UP") for r in rows)
    assert A["GATED"]["mean_r"] > A["CLAUDE"]["mean_r"]
    assert ca["claude_minus_gated"]["diff"] == pytest.approx(A["CLAUDE"]["mean_r"] - A["GATED"]["mean_r"], abs=1e-3)
    ci = ca["claude_minus_gated"]["ci90"]
    assert ci and ci[0] <= ca["claude_minus_gated"]["diff"] <= ci[1]
    assert ca["claude_minus_gated"]["interval_note"] == ""                 # 5 days = MIN_BOOT_DAYS
    # deterministic
    assert ds.control_arms(f) == ca
    # end_day limits a read to its own days
    assert ds.control_arms(f, end_day="2026-11-03")["n_scored"] == 8


def test_control_arms_few_days_note_and_empty(tmp_path):
    f = _folder(tmp_path, days=(2, 3))
    ca = ds.control_arms(f)
    assert "too coarse" in ca["claude_minus_gated"]["interval_note"]
    empty = ds.control_arms(str(tmp_path / "nothing"))
    assert empty["n_scored"] == 0 and empty["claude_minus_gated"]["diff"] is None
    assert "No candidates yet" in "\n".join(ds.a4_lines(empty))
    assert ds.a4_reading(empty).startswith("No reading possible")


def test_reading_matches_addendum_wording():
    mk = lambda lo, hi: {"claude_minus_gated": {"ci90": [lo, hi]}}
    assert "adds something" in ds.a4_reading(mk(0.1, 0.5))
    assert "rule beats Claude" in ds.a4_reading(mk(-0.5, -0.1))
    assert "straddles 0" in ds.a4_reading(mk(-0.2, 0.3)) and "plan usage" in ds.a4_reading(mk(-0.2, 0.3))


def test_running_numbers_carry_no_verdict_words(tmp_path):
    for days in ((2, 3, 4, 5, 6), (2,)):
        f = _folder(tmp_path / str(len(days)), days=days)
        txt = "\n".join(ds.a4_lines(ds.control_arms(f)) + gr.control_arm_lines(f)).upper()
        for w in FORBIDDEN:
            assert w not in txt, w
    txt = "\n".join(ds.a4_lines(ds.control_arms(str(tmp_path / "none")))).upper()
    for w in FORBIDDEN:
        assert w not in txt, w


def test_grade_progress_shows_control_arm_and_stays_verdict_free(tmp_path):
    f = str(tmp_path / "g")
    gr._synth(f, 12, gr.SYN_DAYS, 1, "best")
    text, _ = gr.build_report(f, n_copies=20)
    assert "CONTROL ARM (Addendum A4" in text
    for w in FORBIDDEN:
        assert w not in text.upper()


def test_grade_read_section_adds_reading(tmp_path):
    f = str(tmp_path / "g")
    gr._synth(f, 61, gr.SYN_DAYS, 1, "best")
    text, _ = gr.build_report(f, n_copies=20)
    assert "Read 1" in text and "CONTROL ARM (Addendum A4" in text and "Reading (written in Addendum A4" in text


def test_grade_survives_control_arm_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "control_arms", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gr.control_arm_lines(str(tmp_path))[0].startswith("Control arm (Addendum A4): could not be computed")


def test_decision_score_report_carries_control_arms(tmp_path):
    f = _folder(tmp_path)
    rep, txt = ds.write_report(f)
    assert rep["control_arms"]["n_scored"] == 20 and "CONTROL ARM (Addendum A4" in txt
    j = json.load(open(os.path.join(f, "data", "decision_score.json"), encoding="utf-8"))
    assert j["control_arms"]["arms"]["GATED"]["n"] == 10


def test_page_data_carries_control_arm_summary(tmp_path):
    from test_day_desk import make_desk, breakout_set
    d, _ = make_desk(tmp_path, breakout_set(), [])
    rows = {"header": "h", "n": 1, "days": [], "by_action": {}, "buy_minus_pass": {}, "dodged_losers": 0,
            "missed_winners": 0, "control_arms": {"n_scored": 0, "start_day": "2026-11-02", "dropped": [1, 2]}}
    os.makedirs(os.path.join(d.folder, "data"), exist_ok=True)
    with open(os.path.join(d.folder, "data", "lab_report.json"), "w") as fh:
        json.dump({"n_trades": 0, "friction": {"n": 0}}, fh)
    with open(os.path.join(d.folder, "data", "decision_score.json"), "w") as fh:
        json.dump(rows, fh)
    s = d.lab_summary()
    assert s["decisions"]["control_arms"]["start_day"] == "2026-11-02"
    assert "dropped" not in s["decisions"]["control_arms"] and s["friction"] == {"n": 0}
    page = open(os.path.join(ROOT, "page.html"), encoding="utf-8").read()
    assert "control_arms" in page and 'id="ctlarm"' in page


# ---------------- prompt "facts" audit
def _view():
    return {"cash": 10000.0, "positions": {}}


def test_prompt_never_prints_made_up_numbers():
    zero_vol = flat_day(2, n=20, v=0)
    p = brain.build_prompt("AMD", "x", zero_vol, flat_day(2, price=500.0, n=5), [], _view(), 10)
    assert "VWAP n/a (no volume recorded yet)" in p
    assert "last-15-minute change n/a (only 5 minute(s) of QQQ bars so far)" in p
    assert "+0.00% in the last 15 minutes" not in p
    p = brain.build_prompt("AMD", "x", flat_day(2, n=20), [], [], _view(), 10)
    assert "Nasdaq (QQQ): no bars available right now." in p
    flat = [bar(ts(9, 30) + 60 * i, 100, 100, 100, 100) for i in range(5)]
    p = brain.build_prompt("AMD", "x", flat, flat_day(2, price=500.0, n=20), [], _view(), 10)
    assert "Typical 1-minute range today: n/a" in p
    zero_open = [bar(ts(9, 30), 0.0, 101, 99, 100)] + flat_day(2, n=5)[1:]
    p = brain.build_prompt("AMD", "x", zero_open, flat_day(2, price=500.0, n=20), [], _view(), 10)
    assert "change since the open n/a" in p
    for txt in [p]:
        assert "nan" not in txt.lower().replace("nasdaq", "").replace("financ", "")


def test_prompt_normal_case_unchanged_wording():
    p = brain.build_prompt("AMD", "x", flat_day(2, n=30, drift=0.01), flat_day(2, price=500.0, n=30), [], _view(), 10)
    assert "% since the open, high" in p and "VWAP 100." in p and "% in the last 15 minutes." in p
    assert "Typical 1-minute range today: 0.50%" in p


def test_prompt_error_is_logged_not_lost(tmp_path, monkeypatch):
    from test_day_desk import make_desk, breakout_set
    import day_desk
    d, _ = make_desk(tmp_path, breakout_set(), [])
    monkeypatch.setattr(day_desk, "build_prompt", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad fact")))
    bars = breakout_set()
    d.step(bars["AMD"][-1]["t"] + 65)                            # scanner fires, the prompt build blows up
    assert not d.brain.prompts if hasattr(d.brain, "prompts") else True
    last = d.recent[-1]
    assert last["action"] == "-" and "prompt could not be built" in last["result"] and "bad fact" in last["result"]
    assert d.book.s["calls_today"] == 0                          # no call was made, none counted


# ---------------- friction in R
def test_friction_r_per_trade_and_summary():
    tr = {"sym": "AMD", "entry_t": ts(10, 0), "exit_t": ts(10, 30), "entry": 100.0 * (1 + COST_PER_SIDE),
          "raw_entry": 100.0, "exit": 99.7 * (1 - COST_PER_SIDE), "pnl": -1.2, "pnl_pct": -0.3, "stop": 99.7,
          "target": 101.0, "why_out": "stop hit"}
    r = lab.excursions(tr, flat_day(2, n=90), json.load(open(os.path.join(ROOT, "config.json"))))
    assert r["friction_r"] == pytest.approx(2 * COST_PER_SIDE * 100 / 0.3, rel=1e-3)        # ~0.067 R, "~7% of R"
    assert 0.06 < r["friction_r"] < 0.07
    s = lab.friction_summary([r, dict(r, friction_r=None, stop_pct=None)])
    assert s["n"] == 1 and s["n_no_stop"] == 1 and s["median_r_stress"] == pytest.approx(5 * s["median_r"], rel=1e-3)
    assert lab.friction_summary([]) == {"n": 0, "n_no_stop": 0}
    assert "Friction in R" in lab.friction_lines(s)[0] and "no trades" in lab.friction_lines({"n": 0})[0]
    assert lab.STRESS_COST_PER_SIDE == gr.STRESS_COST


# ---------------- mailbox kill switch
def test_mailbox_pause_and_resume(tmp_path):
    from test_day_desk import make_desk, breakout_set
    d, brain_ = make_desk(tmp_path, breakout_set(), [{"action": "buy", "shares": 10, "stop": 99.0, "target": 103.0,
                                                       "why": "x"}])
    def drop(name, word):
        with open(os.path.join(d.health.inbox, name), "w", encoding="utf-8") as fh:
            fh.write(word)
        d.health.check_inbox()
        return json.load(open(d.health._answer_path(name), encoding="utf-8"))
    a = drop("pause-1031a.txt", "pause")
    assert a["ok"] and a["summary"].startswith("PAUSED") and d.book.s["paused"] is True
    bars = breakout_set()
    d.step(bars["AMD"][-1]["t"] + 65)
    assert not d.book.s["positions"] and not brain_.prompts and d.status["market"] == "open (paused by you)"
    a = drop("resume-1031a.txt", "resume")
    assert a["ok"] and a["summary"].startswith("RESUMED") and "was paused" in a["summary"] and d.book.s["paused"] is False
    assert "pause" in json.dumps(drop("help-1031a.txt", "help"))
