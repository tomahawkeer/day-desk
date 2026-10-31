"""Trade lab: hard facts about Day Desk's own trades, measured from the saved 1-minute bars.

Run LAB.bat (or: python lab.py). It reads data/trades.csv, data/decisions.jsonl and data/bars/*.json,
prints a plain-English report and saves data/lab_report.txt and data/lab_report.json.

It makes NO Claude calls, uses no network, and gives the same answer every time on the same files.
Everything here is HINDSIGHT on a handful of trades: a clue for the end-of-day review, never a rule.

Words used below:
  MFE (max favorable excursion) = the best the trade ever looked while open: highest high vs the buy price, in %.
  MAE (max adverse excursion)   = the worst the trade ever looked while open: lowest low vs the buy price, in %.
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
from datetime import datetime

from engine import COST_PER_SIDE, ET, hm, minutes_et
from health import write_json

HERE = os.path.dirname(os.path.abspath(__file__))

MARKET_OPEN_MINUTE = 9 * 60 + 30          # 9:30 AM ET (the config's own note: 570 = 9:30 AM)
FIRST_HOUR_MINUTES = 60                   # "first hour" = 9:30-10:30 AM
REENTRY_WINDOW_MIN = 60                   # a re-entry = same stock bought within 60 min after a stop-out
JOIN_SECONDS = 5                          # a trade matches its buy record if the times are within 5 s
MULTS = [0.5, 0.75, 1.0, 1.5, 2.0]
DEFAULT_MAX_STOP_PCT = 3.0                # only used if config.json has no max_stop_pct


# ---------------------------------------------------------------- loading
def load_config(folder: str) -> dict:
    path = os.path.join(folder, "config.json")
    if not os.path.exists(path):                  # the data folder is the program folder on the PC; tests differ
        path = os.path.join(HERE, "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_trades(folder: str) -> list[dict]:
    p = os.path.join(folder, "data", "trades.csv")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            r["shares"] = int(float(r["shares"]))
            for k in ("entry", "exit", "entry_t", "exit_t", "pnl", "pnl_pct"):
                r[k] = float(r[k])
        except (KeyError, ValueError, TypeError):
            continue
        r["setup"] = r.get("setup") or ""
        r["why_out"] = r.get("why_out") or ""
        out.append(r)
    out.sort(key=lambda r: (r["entry_t"], r["sym"]))
    return out


def load_buys(folder: str) -> list[dict]:
    p = os.path.join(folder, "data", "decisions.jsonl")
    out = []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("action") == "buy" and "t" in r and "sym" in r:
                out.append(r)
    return out


def attach_plan(trades: list[dict], buys: list[dict]) -> None:
    """Give each trade its initial stop, target and pre-cost buy price from its buy record."""
    for t in trades:
        best = None
        for b in buys:
            if b["sym"] == t["sym"] and abs(float(b["t"]) - t["entry_t"]) < JOIN_SECONDS:
                if best is None or abs(float(b["t"]) - t["entry_t"]) < abs(float(best["t"]) - t["entry_t"]):
                    best = b
        t["stop"] = float(best["stop"]) if best and best.get("stop") is not None else None
        t["target"] = float(best["target"]) if best and best.get("target") is not None else None
        # raw (pre-cost) buy price: from the buy record if present, else undo the cost on the logged fill
        t["raw_entry"] = float(best["price"]) if best and best.get("price") else t["entry"] / (1 + COST_PER_SIDE)


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).date().isoformat()


def load_bars(folder: str, day: str) -> dict:
    p = os.path.join(folder, "data", "bars", f"{day}.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("bars", {}) or {}
    except ValueError:
        return {}


def exit_reason(why_out: str) -> str:
    w = (why_out or "").lower()
    if w.startswith("stop hit"):
        return "stop"
    if w.startswith("target hit"):
        return "target"
    if "end-of-day" in w or "close-out" in w:
        return "close-out"
    if "overnight" in w:
        return "left open"
    if "sell all" in w or "by you" in w:
        return "you sold"
    return "Claude sold"


# ---------------------------------------------------------------- pure functions
def walk_exits(bars: list[dict], stop: float, target: float | None, since: float) -> dict | None:
    """The engine's resting-order rule (engine.Book.check_exits), as a pure function.
    Walk bars that opened after `since`, oldest first. Stop fills at the stop, or at the bar's open if
    price gapped through it; target likewise. A bar touching both counts as the stop."""
    for b in bars:
        if b["t"] <= since:
            continue
        if b["l"] <= stop:
            return {"price": min(stop, b["o"]), "t": b["t"], "reason": "stop"}
        if target is not None and b["h"] >= target:
            return {"price": max(target, b["o"]), "t": b["t"], "reason": "target"}
    return None


def _pct(x: float, base: float) -> float:
    return (x / base - 1) * 100


def _flat_bars(bars: list[dict], flatten_minute: int) -> list[dict]:
    return [b for b in bars if minutes_et(b["t"]) < flatten_minute]


def excursions(trade: dict, bars_for_sym: list[dict], cfg: dict | None = None) -> dict:
    """Measure one trade on its stock's 1-minute bars (all % are vs the raw, pre-cost buy price)."""
    cfg = cfg or load_config(HERE)
    flat_min = cfg["flatten_minute"]
    bars = sorted(bars_for_sym or [], key=lambda b: b["t"])
    e_t, x_t = trade["entry_t"], trade["exit_t"]
    raw = trade.get("raw_entry") or trade["entry"] / (1 + COST_PER_SIDE)
    raw_exit = trade["exit"] / (1 - COST_PER_SIDE)
    # In-trade bars: opened after the buy (as the engine checks them), and either fully before the
    # sale or the very bar the stop/target filled on (its open time IS the exit time).
    inside = [b for b in bars if b["t"] > e_t and (b["t"] + 60 <= x_t or b["t"] == x_t)]
    after = [b for b in _flat_bars(bars, flat_min) if b["t"] > x_t]      # bars that opened after the sale, up to 3:45
    morning = [b for b in bars if b["t"] + 60 <= e_t and minutes_et(b["t"]) >= MARKET_OPEN_MINUTE]
    if not morning:                                   # bought in the first minute: use that bar alone
        morning = [b for b in bars if b["t"] <= e_t][-1:]
    typ = statistics.fmean([(b["h"] - b["l"]) / b["o"] * 100 for b in morning]) if morning else None

    hi = max([b["h"] for b in inside] + [raw_exit])
    lo = min([b["l"] for b in inside] + [raw_exit])
    stop, target = trade.get("stop"), trade.get("target")
    stop_pct = (raw - stop) / raw * 100 if stop is not None else None
    out = {
        "sym": trade["sym"], "day": day_of(e_t), "setup": trade.get("setup", ""),
        "entry_time": hm(e_t), "exit_time": hm(x_t),
        "first_hour": minutes_et(e_t) < MARKET_OPEN_MINUTE + FIRST_HOUR_MINUTES,
        "held_min": round((x_t - e_t) / 60, 1),
        "pnl": round(trade["pnl"], 2), "pnl_pct": round(trade["pnl_pct"], 3),
        "exit_reason": exit_reason(trade.get("why_out", "")),
        "mfe_pct": round(_pct(hi, raw), 3), "mae_pct": round(_pct(lo, raw), 3),
        "bars_in_trade": len(inside),
        "stop_pct": round(stop_pct, 3) if stop_pct is not None else None,
        "target_pct": round(_pct(target, raw), 3) if target is not None else None,
        "typical_1m_range_pct": round(typ, 4) if typ else None,
        "stop_in_typical_ranges": round(stop_pct / typ, 2) if (stop_pct is not None and typ) else None,
        "after_max_high_pct": None, "after_min_low_pct": None, "close_at_flatten_pct": None,
        "after_hit_target": None, "stopped_then_recovered": None,
    }
    if after:
        out["after_max_high_pct"] = round(_pct(max(b["h"] for b in after), raw), 3)
        out["after_min_low_pct"] = round(_pct(min(b["l"] for b in after), raw), 3)
        out["close_at_flatten_pct"] = round(_pct(after[-1]["c"], raw), 3)
        if target is not None:
            out["after_hit_target"] = max(b["h"] for b in after) >= target
        if out["exit_reason"] == "stop":
            out["stopped_then_recovered"] = bool(out["after_hit_target"])
    return out


def stop_whatif(trade: dict, bars: list[dict], stop_mult: float, cfg: dict | None = None) -> dict:
    """Replay the same buy with the stop distance x stop_mult (target unchanged) under the engine's
    fill rules; anything still open is sold at the 3:45 close-out (close of the last bar before it)."""
    cfg = cfg or load_config(HERE)
    cap = float(cfg.get("max_stop_pct", DEFAULT_MAX_STOP_PCT))
    raw = trade.get("raw_entry") or trade["entry"] / (1 + COST_PER_SIDE)
    if trade.get("stop") is None:
        return {"ok": False, "note": "no initial stop on record for this trade"}
    dist = (raw - trade["stop"]) * stop_mult
    new_stop = raw - dist
    floor = raw * (1 - cap / 100)
    clamped = new_stop < floor - 1e-12
    if clamped:
        new_stop = floor
    target = trade.get("target")
    day_bars = _flat_bars(sorted(bars or [], key=lambda b: b["t"]), cfg["flatten_minute"])
    hit = walk_exits(day_bars, new_stop, target, trade["entry_t"])
    if hit:
        px, reason, t = hit["price"], hit["reason"], hit["t"]
    else:
        later = [b for b in day_bars if b["t"] > trade["entry_t"]]
        if later:
            px, t = later[-1]["c"], later[-1]["t"] + 60
        else:
            px, t = raw, trade["entry_t"]
        reason = "close-out"
    fill = px * (1 - COST_PER_SIDE)
    pnl = (fill - trade["entry"]) * trade["shares"]
    return {"ok": True, "stop_mult": stop_mult, "stop": round(new_stop, 4),
            "stop_pct": round((raw - new_stop) / raw * 100, 3),
            "clamped": clamped,
            "note": f"stop clamped to the {cap:g}% hard cap" if clamped else "",
            "exit_price": round(px, 4), "exit_reason": reason, "exit_time": hm(t),
            "pnl": round(pnl, 2)}


# ---------------------------------------------------------------- report
def _bucket_table(rows: list[dict], key) -> dict:
    """Total what-if P&L and stop-outs per multiplier for the trades where key(row) is true."""
    sel = [r for r in rows if key(r) and r["whatif"]]
    tab = {}
    for m in MULTS:
        w = [r["whatif"][str(m)] for r in sel]
        tab[str(m)] = {"n": len(w), "pnl": round(sum(x["pnl"] for x in w), 2),
                       "stop_outs": sum(1 for x in w if x["exit_reason"] == "stop"),
                       "clamped": sum(1 for x in w if x["clamped"])}
    actual = round(sum(r["pnl"] for r in sel), 2)
    return {"n": len(sel), "actual_pnl": actual, "by_mult": tab}


def _noise(rows: list[dict], table: dict) -> dict:
    pnls = [r["pnl"] for r in rows if r["whatif"]]
    n = len(pnls)
    totals = {m: v["pnl"] for m, v in table["by_mult"].items()}
    if n < 2:
        return {"n": n, "verdict": "too few trades to judge", "noise": None}
    noise = statistics.stdev(pnls) * math.sqrt(n)
    base = totals["1.0"]
    best_m = max(totals, key=lambda m: (totals[m], -abs(float(m) - 1)))
    diff = totals[best_m] - base
    spread = max(totals.values()) - min(totals.values())
    return {"n": n, "noise": round(noise, 2), "spread": round(spread, 2), "best_mult": best_m,
            "best_minus_base": round(diff, 2),
            "verdict": "within noise" if abs(diff) < noise else "NOT within noise (still hindsight)"}


def analyse(folder: str) -> list[dict]:
    """Every trade that has bars, with its excursions and stop what-ifs."""
    cfg = load_config(folder)
    trades = load_trades(folder)
    attach_plan(trades, load_buys(folder))
    cache: dict[str, dict] = {}
    rows = []
    prev: list[dict] = []
    for t in trades:
        d = day_of(t["entry_t"])
        if d not in cache:
            cache[d] = load_bars(folder, d)
        bars = cache[d].get(t["sym"])
        reentry = any(p["sym"] == t["sym"] and exit_reason(p["why_out"]) == "stop"
                      and 0 <= t["entry_t"] - p["exit_t"] <= REENTRY_WINDOW_MIN * 60 for p in prev)
        prev.append(t)
        if not bars:
            rows.append({"sym": t["sym"], "day": d, "pnl": round(t["pnl"], 2), "no_bars": True,
                         "reentry_after_stop": reentry, "whatif": None})
            continue
        r = excursions(t, bars, cfg)
        r["reentry_after_stop"] = reentry
        r["whatif"] = None
        if t.get("stop") is not None:
            r["whatif"] = {str(m): stop_whatif(t, bars, m, cfg) for m in MULTS}
        rows.append(r)
    return rows


def lab_report(folder: str) -> dict:
    rows_all = analyse(folder)
    rows = [r for r in rows_all if not r.get("no_bars")]
    n = len(rows)
    allt = _bucket_table(rows, lambda r: True)
    fh = _bucket_table(rows, lambda r: r["first_hour"])
    lt = _bucket_table(rows, lambda r: not r["first_hour"])
    re_rows = [r for r in rows_all if r["reentry_after_stop"]]
    stops = [r for r in rows if r["exit_reason"] == "stop"]
    caveat = (f"HINDSIGHT on n={allt['n']} trades. n is tiny. Picking the best multiplier from this table is "
              f"curve-fitting: it is evidence for a hypothesis, not a rule.")
    return {
        "n_trades": len(rows_all), "n_with_bars": n,
        "skipped_no_bars": [f"{r['sym']} {r['day']}" for r in rows_all if r.get("no_bars")],
        "n_no_plan": sum(1 for r in rows if r["whatif"] is None),
        "trades": rows,
        "stop_multipliers": {"caveat": caveat, "all": allt, "noise_check": _noise(rows, allt),
                             "first_hour": fh, "later": lt},
        "stopped_then_recovered": {"n_stops": len(stops),
                                   "n_recovered": sum(1 for r in stops if r["stopped_then_recovered"])},
        "reentry_after_stop": {"n": len(re_rows), "pnl": round(sum(r["pnl"] for r in re_rows), 2),
                               "trades": [f"{r['sym']} {r['day']} {r.get('entry_time', '')} {r['pnl']:+.2f}"
                                          for r in re_rows]},
    }


def _money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def _table_lines(title: str, tab: dict) -> list[str]:
    L = [f"  {title} (n={tab['n']}, actual total {_money(tab['actual_pnl'])})"]
    L.append(f"    {'stop x':>7}  {'what-if total':>14}  {'stop-outs':>10}")
    for m, v in tab["by_mult"].items():
        cl = f"  ({v['clamped']} clamped at the cap)" if v["clamped"] else ""
        L.append(f"    {float(m):>6.2f}x  {_money(v['pnl']):>14} (n={v['n']})  {v['stop_outs']:>3} of n={v['n']}{cl}")
    return L


def report_text(rep: dict) -> str:
    sm = rep["stop_multipliers"]
    nc = sm["noise_check"]
    L = ["DAY DESK TRADE LAB  (no Claude calls; measured from saved 1-minute bars)", "",
         "MFE = the best the trade ever looked while open (highest price vs the buy price, %).",
         "MAE = the worst the trade ever looked while open (lowest price vs the buy price, %).",
         "Stop width 'xR' = how far the stop was, in units of the stock's typical 1-minute range that morning.",
         "", f"Trades: {rep['n_trades']} in the log, {rep['n_with_bars']} with saved bars."]
    if rep["skipped_no_bars"]:
        L.append("  No bars saved for: " + ", ".join(rep["skipped_no_bars"]))
    L += ["", "PER TRADE"]
    for r in rep["trades"]:
        aft = ""
        if r["after_max_high_pct"] is not None:
            aft = (f" | after exit: high {r['after_max_high_pct']:+.2f}%, low {r['after_min_low_pct']:+.2f}%, "
                   f"3:45 {r['close_at_flatten_pct']:+.2f}%")
            if r["stopped_then_recovered"]:
                aft += "  << stopped, then reached the target"
        xr = f"{r['stop_in_typical_ranges']:.1f}xR" if r["stop_in_typical_ranges"] is not None else "?xR"
        sp = f"{r['stop_pct']:.2f}%" if r["stop_pct"] is not None else "?"
        L.append(f"  {r['day']} {r['entry_time']:>8} {r['sym']:<5} {r['setup'][:14]:<14} held {r['held_min']:>5.0f}m "
                 f"{_money(r['pnl']):>9}  MFE {r['mfe_pct']:+.2f}% MAE {r['mae_pct']:+.2f}%  stop {sp} ({xr}) "
                 f"-> {r['exit_reason']}{aft}")
    L += ["", "STOP-WIDTH WHAT-IF (same entries, stop distance x multiplier, target unchanged, engine fill rules,",
          "anything open sold at 3:45)", "  !! " + sm["caveat"]]
    L += _table_lines("All trades", sm["all"])
    if nc.get("noise") is not None:
        L.append(f"  Noise check (n={nc['n']}): best = x{nc['best_mult']}, {_money(nc['best_minus_base'])} vs x1.0 replay; "
                 f"spread of totals ${nc['spread']:,.2f}; noise yardstick (stdev of per-trade P&L x sqrt(n)) "
                 f"${nc['noise']:,.2f} -> {nc['verdict']}.")
    else:
        L.append(f"  Noise check (n={nc['n']}): {nc['verdict']}.")
    L += ["", "  !! " + sm["caveat"].replace(f"n={sm['all']['n']}", f"n={sm['first_hour']['n']}")]
    L += _table_lines("First-hour entries (9:30-10:30)", sm["first_hour"])
    L += ["  !! " + sm["caveat"].replace(f"n={sm['all']['n']}", f"n={sm['later']['n']}")]
    L += _table_lines("Later entries (after 10:30)", sm["later"])
    s = rep["stopped_then_recovered"]
    L += ["", f"Stopped, then price reached the original target before 3:45: {s['n_recovered']} of n={s['n_stops']} stop-outs."]
    ra = rep["reentry_after_stop"]
    L.append(f"Re-entries within {REENTRY_WINDOW_MIN} min of a stop-out in the same stock: n={ra['n']}, total {_money(ra['pnl'])}"
             + (": " + "; ".join(ra["trades"]) if ra["trades"] else ""))
    if rep["n_no_plan"]:
        L.append(f"{rep['n_no_plan']} trade(s) had no matching buy record, so no what-if for them.")
    L += ["", f"Hindsight on {rep['n_with_bars']} trades — a clue, not proof."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- prompt helpers
def review_facts(folder: str, day: str, max_lines: int = 25, max_chars: int = 2500) -> str:
    """Compact facts for the end-of-day review prompt. "" if no trades that day."""
    rep = lab_report(folder)
    todays = [r for r in rep["trades"] if r["day"] == day]
    if not todays:
        return ""
    lines = []
    for r in todays:
        xr = f"{r['stop_in_typical_ranges']:.1f}xR" if r["stop_in_typical_ranges"] is not None else "?xR"
        s = (f"{r['sym']} {r['setup'] or 'untagged'} {r['entry_time']} held {r['held_min']:.0f}m {_money(r['pnl'])} "
             f"MFE {r['mfe_pct']:+.1f}% MAE {r['mae_pct']:+.1f}% stop {xr} -> {r['exit_reason']}")
        # the after-exit note only when it matters: price later beat the trade's best, or reached the target
        if r["after_max_high_pct"] is not None and (r["after_hit_target"] or r["after_max_high_pct"] > r["mfe_pct"]):
            word = "stopped" if r["exit_reason"] == "stop" else "sold"
            s += (f"; {word} {r['pnl_pct']:+.1f}%, then rose to {r['after_max_high_pct']:+.1f}% before 3:45"
                  f"{' (hit target)' if r['after_hit_target'] else ''}, 3:45 close {r['close_at_flatten_pct']:+.1f}%")
        lines.append(s)
    sm = rep["stop_multipliers"]
    a, nc, fh = sm["all"], sm["noise_check"], sm["first_hour"]
    n = a["n"]
    d15 = a["by_mult"]["1.5"]["pnl"] - a["by_mult"]["1.0"]["pnl"] if n else 0.0
    noise_word = "within noise" if (nc.get("noise") is not None and abs(d15) < nc["noise"]) else \
        ("too few to judge" if nc.get("noise") is None else "not within noise")
    ra = rep["reentry_after_stop"]
    tail = [f"Across all {n} trades: stop x1.5 would have changed total by {_money(d15)} vs x1.0 replay ({noise_word}); "
            f"first-hour entries: n={fh['n']}, {_money(fh['actual_pnl'])}; re-entries after a stop: n={ra['n']}, {_money(ra['pnl'])}",
            f"Hindsight on {n} trades — a clue, not proof."]
    head = [f"Trade lab facts for {day} (measured from 1-min bars; xR = stop width in typical 1-min ranges):"]
    body = lines
    while True:
        extra = [f"(+{len(lines) - len(body)} more trades not shown)"] if len(body) < len(lines) else []
        out = head + body + extra + tail
        txt = "\n".join(out)
        if (len(out) <= max_lines and len(txt) <= max_chars) or not body:
            return txt[:max_chars] if len(txt) > max_chars else txt
        body = body[:-1]


def symbol_today(folder: str, sym: str, day: str, now_t: float, last_price: float | None = None) -> str:
    """One line for the live decision prompt about earlier trades today in this stock. "" if none."""
    parts = []
    for t in load_trades(folder):
        if t["sym"] != sym or day_of(t["exit_t"]) != day or t["exit_t"] > now_t:
            continue
        why = exit_reason(t["why_out"])
        verb = {"stop": "stopped out of", "target": "took profit in"}.get(why, "sold")
        s = f"{verb} {sym} at {hm(t['exit_t'])} ({_money(t['pnl'])})"
        parts.append((t["exit_t"], s, t["exit"] / (1 - COST_PER_SIDE)))
    if not parts:
        return ""
    parts.sort()
    line = "Earlier today: " + "; ".join(p[1] for p in parts)
    if last_price:
        line += f", price since then {_pct(last_price, parts[-1][2]):+.1f}%"
    return line


# ---------------------------------------------------------------- CLI
def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_report(folder: str = HERE) -> tuple[dict, str]:
    """Compute the full lab report and save data/lab_report.txt + .json (the page reads the JSON)."""
    rep = lab_report(folder)
    rep["updated_t"] = datetime.now(ET).timestamp()
    txt = report_text(rep)
    _write_text(os.path.join(folder, "data", "lab_report.txt"), txt)
    write_json(os.path.join(folder, "data", "lab_report.json"), rep, indent=1)
    return rep, txt


def main(folder: str = HERE) -> None:
    _rep, txt = write_report(folder)
    print(txt)
    print("Saved data/lab_report.txt and data/lab_report.json")


if __name__ == "__main__":
    main()
