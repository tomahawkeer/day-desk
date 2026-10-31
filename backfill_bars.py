"""Save the 1-minute bars of past trading days that are not in the archive yet.

Day Desk saves every day's 1-minute bars to data/bars/<day>.json at the 3:45 PM close-out (added 10-30).
Days before that were never saved, and the pre-registered grade and the trigger lab need them. Yahoo keeps
1-minute bars for only about 30 days, and hands out at most about a week per request.

  python backfill_bars.py            (BACKFILL_BARS.bat) the last 7 days in one request, as before
  python backfill_bars.py --days 30  (BACKFILL_30D.bat)  up to the last 30 calendar days, fetched in chunks of
                                     at most 7 days each (Yahoo's limit), one request at a time

It is safe to run any number of times: it never overwrites a day that already has a file, and it never writes
today (the running program does that at 3:45). Each saved file records where it came from ("source") and how
many bars each stock got ("bar_counts"). Downloads are single-threaded (threads=False), one after another.
Don't run it at the same moment as a feed self-test; outside market hours is best.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime, timedelta, timezone

from engine import ET
from health import write_json

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

SESSION_START_MIN = 570          # 9:30 AM ET: regular session only, like the live feed
SESSION_END_MIN = 960            # 4:00 PM ET (exclusive)
CHUNK_DAYS = 7                   # Yahoo: at most ~7 days of 1-minute bars per request
MAX_DAYS = 30                    # Yahoo: 1-minute bars only exist for about the last 30 days


def symbols(data_dir: str = DATA) -> list[str]:
    syms = {"QQQ"}
    p = os.path.join(data_dir, "trades.csv")
    if os.path.exists(p):
        syms |= {r["sym"] for r in csv.DictReader(open(p, encoding="utf-8"))}
    p = os.path.join(data_dir, "watchlist.json")
    if os.path.exists(p):
        syms |= set(json.load(open(p, encoding="utf-8")).get("symbols", []))
    p = os.path.join(data_dir, "picks_history.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                syms |= {x["ticker"] for x in json.loads(line).get("picks", []) if x.get("ticker")}
            except Exception:
                pass
    return sorted(syms)


def chunks(today: date, days: int, size: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    """[start, end) date windows covering the `days` calendar days up to and including today, oldest first,
    each at most `size` days long (yfinance's end date is exclusive)."""
    days = max(1, min(int(days), MAX_DAYS))
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    out = []
    s = start
    while s < end:
        e = min(s + timedelta(days=size), end)
        out.append((s, e))
        s = e
    return out


def frame_to_days(df, syms: list[str], ys: list[str], days: dict | None = None,
                  report=print) -> dict[str, dict[str, list]]:
    """Turn a yfinance multi-ticker frame into {day: {sym: [bars]}} (regular session only), merged into `days`.
    Bars already present for that stock and minute are not duplicated."""
    days = {} if days is None else days
    for s, y in zip(syms, ys):
        try:
            sub = df[y].dropna(how="any")
        except Exception:
            report(f"  {s}: no data from Yahoo")
            continue
        for ts, row in sub.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            et = t.astimezone(ET)
            m = et.hour * 60 + et.minute
            if m < SESSION_START_MIN or m >= SESSION_END_MIN:
                continue
            days.setdefault(et.date().isoformat(), {}).setdefault(s, []).append(
                {"t": int(t.timestamp()), "o": float(row["Open"]), "h": float(row["High"]),
                 "l": float(row["Low"]), "c": float(row["Close"]), "v": float(row["Volume"])})
    for d in days.values():                          # tidy: oldest first, one bar per minute
        for s, bars in d.items():
            seen, clean = set(), []
            for b in sorted(bars, key=lambda b: b["t"]):
                if b["t"] not in seen:
                    seen.add(b["t"]); clean.append(b)
            d[s] = clean
    return days


def save_days(days: dict, today: str, source: str, data_dir: str = DATA, report=print) -> list[str]:
    """Write each day not yet on disk (never today, never overwrite). Returns the days written."""
    os.makedirs(os.path.join(data_dir, "bars"), exist_ok=True)
    written = []
    for day in sorted(days):
        path = os.path.join(data_dir, "bars", day + ".json")
        if day >= today:
            report(f"  {day}: skipped (today; the program saves it at 3:45 PM)")
        elif os.path.exists(path):
            report(f"  {day}: already saved, left alone")
        else:
            counts = {s: len(b) for s, b in sorted(days[day].items())}
            write_json(path, {"day": day, "bars": days[day], "source": source, "bar_counts": counts})
            report(f"  {day}: saved {len(counts)} symbols, {sum(counts.values())} bars ({source})")
            written.append(day)
    return written


def _download(yf, ys: list[str], **kw):
    return yf.download(" ".join(ys), interval="1m", group_by="ticker", progress=False, prepost=False,
                       auto_adjust=False, threads=False, multi_level_index=True, **kw)


def run(days: int | None = None, yf=None, today: date | None = None, data_dir: str = DATA,
        syms: list[str] | None = None, report=print) -> list[str]:
    """days=None: the original single 7-day request. days=N: N calendar days in chunks of <= 7 days."""
    if yf is None:
        import yfinance as yf
    today = today or datetime.now(ET).date()
    syms = syms or symbols(data_dir)
    ys = [s.replace(".", "-") for s in syms]                   # BRK.B -> BRK-B on Yahoo
    got: dict = {}
    if days is None:
        report("Fetching the last 7 days of 1-minute bars for: " + ", ".join(syms))
        frame_to_days(_download(yf, ys, period="7d"), syms, ys, got, report)
        source = "backfill_bars.py (Yahoo period=7d)"
    else:
        win = chunks(today, days)
        report(f"Fetching up to the last {min(days, MAX_DAYS)} days of 1-minute bars in {len(win)} chunk(s) for: "
               + ", ".join(syms))
        for s, e in win:
            report(f" chunk {s} .. {e - timedelta(days=1)}")
            try:
                df = _download(yf, ys, start=s.isoformat(), end=e.isoformat())
            except Exception as ex:                             # one bad chunk must not lose the others
                report(f"  chunk failed: {ex}")
                continue
            frame_to_days(df, syms, ys, got, report)
        source = f"backfill_bars.py (Yahoo 1m, --days {days}, chunks of <= {CHUNK_DAYS} days)"
    written = save_days(got, today.isoformat(), source, data_dir, report)
    report(f"Done. {len(written)} new day file(s).")
    return written


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Save past days of 1-minute bars into data/bars (never overwrites).")
    ap.add_argument("--days", type=int, default=None,
                    help=f"fetch up to this many calendar days back (max {MAX_DAYS}) in chunks of <= {CHUNK_DAYS} days")
    a = ap.parse_args(argv)
    run(a.days)


if __name__ == "__main__":
    main()
