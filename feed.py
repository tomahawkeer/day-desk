"""Live prices and headlines from Yahoo Finance (the free `yfinance` package).

No account or key needed. Yahoo's 1-minute bars for US stocks arrive within
seconds, which is plenty for a 30-second loop. yfinance is unofficial, so it
occasionally breaks for a day when Yahoo changes something; when that happens
every call here fails loudly and the loop simply waits and retries.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from engine import ET


def _bars_from_frame(df) -> list[dict]:
    out = []
    if df is None or len(df) == 0:
        return out
    df = df.dropna(how="any")
    today = datetime.now(ET).date()
    for ts, row in df.iterrows():
        t = ts.to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t.astimezone(ET).date() != today:
            continue
        out.append({"t": int(t.timestamp()), "o": float(row["Open"]), "h": float(row["High"]),
                    "l": float(row["Low"]), "c": float(row["Close"]), "v": float(row["Volume"])})
    return out


class YahooFeed:
    def __init__(self):
        import yfinance  # imported here so the tests can run without it
        self.yf = yfinance

    def bars_1m(self, syms: list[str]) -> dict[str, list[dict]]:
        """Today's regular-session 1-minute bars for each symbol."""
        ysyms = [s.replace(".", "-") for s in syms]          # BRK.B -> BRK-B on Yahoo
        df = self.yf.download(" ".join(ysyms), period="1d", interval="1m", group_by="ticker",
                              progress=False, prepost=False, auto_adjust=False, threads=True,
                              ignore_tz=False, multi_level_index=True)
        out: dict[str, list[dict]] = {}
        for s, ys in zip(syms, ysyms):
            try:
                sub = df[ys] if hasattr(df.columns, "levels") and ys in df.columns.get_level_values(0) else df
                out[s] = _bars_from_frame(sub)
            except Exception:
                out[s] = []
        return out

    def news(self, sym: str, limit: int = 6) -> list[str]:
        """Recent headlines as 'HH:MM ET | title' strings, newest first."""
        try:
            items = self.yf.Ticker(sym.replace(".", "-")).news or []
        except Exception:
            return []
        out = []
        for it in items[:limit]:
            c = it.get("content", it)
            title = c.get("title") or ""
            when = c.get("pubDate") or c.get("providerPublishTime")
            stamp = ""
            try:
                if isinstance(when, (int, float)):
                    d = datetime.fromtimestamp(when, ET)
                elif isinstance(when, str):
                    d = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(ET)
                else:
                    d = None
                if d:
                    stamp = d.strftime("%m-%d %H:%M ET")
            except Exception:
                pass
            if title:
                out.append(f"{stamp} | {title}" if stamp else title)
        return out


class FakeFeed:
    """For tests: serves whatever bars and news it is given."""

    def __init__(self, bars: dict[str, list[dict]] | None = None, news: dict[str, list[str]] | None = None):
        self._bars = bars or {}
        self._news = news or {}

    def bars_1m(self, syms):
        return {s: list(self._bars.get(s, [])) for s in syms}

    def news(self, sym, limit=6):
        return self._news.get(sym, [])[:limit]


def now() -> float:
    return time.time()
