"""The paper book: cash, positions, fills, limits, and the trade log.

Nothing here talks to a broker. Every fill is pretend money at the live price
plus a small cost. Every rule that protects the account is enforced HERE, in
code, so it holds no matter what Claude answers.
"""
from __future__ import annotations

import csv
import json
import math
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Cost per side. Robinhood charges no commission; what you really pay is the
# bid/ask spread. For liquid large caps in ~$2,500 orders that is about
# 0.005-0.01%, so 0.01% each way is a fair, slightly cautious number.
COST_PER_SIDE = 0.0001

TRADE_FIELDS = ["sym", "shares", "entry", "exit", "entry_t", "exit_t",
                "pnl", "pnl_pct", "why_in", "why_out", "setup"]


def scorecard(trades: list[dict]) -> list[dict]:
    """Results per strategy ('setup'), worked out in code from the closed trades."""
    by: dict[str, list[dict]] = {}
    for t in trades:
        by.setdefault(t.get("setup") or "untagged", []).append(t)
    rows = []
    for name, ts in by.items():
        wins = [t for t in ts if t["pnl"] > 0]
        rows.append({"setup": name, "trades": len(ts), "wins": len(wins),
                     "win_rate": round(100 * len(wins) / len(ts)),
                     "pnl": round(sum(t["pnl"] for t in ts), 2),
                     "avg_pct": round(sum(t["pnl_pct"] for t in ts) / len(ts), 3),
                     "days": len({datetime.fromtimestamp(t["exit_t"], ET).date() for t in ts})})
    return sorted(rows, key=lambda r: (-r["trades"], r["setup"]))


def et_now(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(ts, ET) if ts is not None else datetime.now(ET)


def hm(ts: float) -> str:
    """'1:07 PM' style Eastern time, the same on Windows and Linux."""
    d = et_now(ts)
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def minutes_et(ts: float) -> int:
    """Minutes since midnight, Eastern time, for a unix timestamp."""
    d = et_now(ts)
    return d.hour * 60 + d.minute


BAR_SECONDS = 60          # 1-minute bars: a bar opened at t is finished at t + 60

# THE FROZEN MONEY LIMITS (pre-registration PART 2). config.json is edited by hand in Notepad, and a deleted or
# zeroed key would silently switch a limit off (day_loss_hit treats a missing pct as "no breaker"). So these are
# the values the pre-registered test runs under; if config.json differs at startup the desk PAUSES new buys and
# says why on the page and in the heartbeat (idea from hummingbot's start-up checks + nautilus's HALTED state).
# Changing one is an amendment: write it in the pre-registration first, then change it here AND in config.json.
FROZEN_LIMITS = {"start_cash": 10000, "max_positions": 4, "max_position_usd": 2500, "max_stop_pct": 3.0,
                 "last_buy_minute": 930, "flatten_minute": 945, "max_daily_loss_pct": 2.0}
BOOKS_TOLERANCE_USD = 0.05            # cash vs trade history may differ by rounding only...
BOOKS_TOLERANCE_PER_TRADE_USD = 0.01  # ...and each trade's P&L is rounded to the cent, so allow that much drift per trade


def limits_problems(cfg: dict) -> list[str]:
    """Plain-English list of frozen limits that config.json changed or removed (empty = all good)."""
    out = []
    for k, v in FROZEN_LIMITS.items():
        got = cfg.get(k)
        if got is None or float(got) != float(v):
            out.append(f"{k} is {got!r} in config.json but the pre-registered value is {v}")
    return out


class Book:
    """The paper account. Thread-safe: the trading loop and the web page share it."""

    def __init__(self, folder: str, cfg: dict):
        self.folder = folder
        self.cfg = cfg
        self.path = os.path.join(folder, "data", "state.json")
        self.trades_path = os.path.join(folder, "data", "trades.csv")
        os.makedirs(os.path.join(folder, "data"), exist_ok=True)
        self.lock = threading.RLock()
        self.s = self._load()
        self.repaired = self._repair_double_sale()

    def _repair_double_sale(self) -> list[str]:
        """sell() appends the trade row, then saves state. A crash between the two left the position 'open' in
        state.json although it was already sold, and the next start would sell it AGAIN (a duplicate trade). Found
        by a code review against nautilus's position reconciliation. Here: any open position whose buy time
        matches a recorded trade is closed out from that record instead."""
        fixed = []
        rows = {(t["sym"], round(t["entry_t"], 3)): t for t in self.trades()}
        for sym, p in list(self.s["positions"].items()):
            t = rows.get((sym, round(p["entry_t"], 3)))
            if t:
                self.s["positions"].pop(sym)
                self.s["cash"] = round(self.s["cash"] + t["shares"] * t["exit"], 4)
                fixed.append(f"{sym} was already sold at {t['exit']:.2f} (state.json missed it); fixed")
        if fixed:
            self.save()
        return fixed

    def books_problem(self) -> str | None:
        """Cash must equal start cash + every closed trade's P&L - what the open positions cost."""
        expect = (self.s["start_cash"] + sum(t["pnl"] for t in self.trades())
                  - sum(p["shares"] * p["entry"] for p in self.s["positions"].values()))
        diff = self.s["cash"] - expect
        if abs(diff) > BOOKS_TOLERANCE_USD + BOOKS_TOLERANCE_PER_TRADE_USD * len(self.trades()):
            return f"cash is ${self.s['cash']:,.2f} but the trade history says ${expect:,.2f} (off by ${diff:+,.2f})"
        return None

    # ---------- persistence ----------
    def _fresh(self) -> dict:
        return {
            "start_cash": float(self.cfg["start_cash"]),
            "cash": float(self.cfg["start_cash"]),
            "positions": {},          # sym -> {shares, entry, entry_t, stop, target, why, last}
            "day": None,              # "YYYY-MM-DD" (ET) of the current session
            "day_start_equity": float(self.cfg["start_cash"]),
            "calls_today": 0,
            "paused": False,
            "last_bar_t": {},         # sym -> unix time of the last 1m bar already checked for exits
        }

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                s = json.load(f)
            base = self._fresh()
            base.update(s)
            return base
        return self._fresh()

    def save(self) -> None:
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.s, f, indent=1)
            os.replace(tmp, self.path)

    def trades(self) -> list[dict]:
        if not os.path.exists(self.trades_path):
            return []
        with open(self.trades_path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["setup"] = r.get("setup") or ""
            for k in ("shares",):
                r[k] = int(float(r[k]))
            for k in ("entry", "exit", "entry_t", "exit_t", "pnl", "pnl_pct"):
                r[k] = float(r[k])
        return rows

    def _append_trade(self, tr: dict) -> None:
        if os.path.exists(self.trades_path):
            with open(self.trades_path, encoding="utf-8", newline="") as f:
                header = f.readline().strip().split(",")
            if header != TRADE_FIELDS:                 # file from an older version: rewrite with the new columns
                old = self.trades()
                os.remove(self.trades_path)
                for r in old:
                    self._append_trade({k: r.get(k, "") for k in TRADE_FIELDS})
        new = not os.path.exists(self.trades_path)
        with open(self.trades_path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow(tr)

    # ---------- numbers ----------
    def equity(self, prices: dict[str, float] | None = None) -> float:
        with self.lock:
            v = self.s["cash"]
            for sym, p in self.s["positions"].items():
                px = (prices or {}).get(sym) or p.get("last") or p["entry"]
                v += p["shares"] * px
            return round(v, 2)

    # ---------- the rules ----------
    def can_buy(self, sym: str, price: float, shares: int, stop: float, target: float,
                now_ts: float, watchlist: list[str]) -> str | None:
        """Return None if allowed, else the reason it is refused (plain English)."""
        c = self.cfg
        m = minutes_et(now_ts)
        if self.s["paused"]:
            return "trading is paused"
        if self.day_loss_hit():
            return f"daily loss limit reached ({c['max_daily_loss_pct']}% down today): no new buys until tomorrow"
        if sym not in watchlist:
            return f"{sym} is not on the watchlist"
        if m < 9 * 60 + 31:
            return "market not open yet"
        if m >= c["last_buy_minute"]:
            return "too late in the day for new buys"
        if sym in self.s["positions"]:
            return f"already holding {sym}"
        if len(self.s["positions"]) >= c["max_positions"]:
            return f"already at {c['max_positions']} positions"
        if not (shares >= 1):
            return "share count below 1"
        if not (stop < price < target):
            return "stop must be below the price and target above it"
        if stop < price * (1 - c["max_stop_pct"] / 100):
            return f"stop more than {c['max_stop_pct']}% away"
        fill = price * (1 + COST_PER_SIDE)
        if shares * fill > c["max_position_usd"] + 1e-6:
            return f"order above ${c['max_position_usd']:,.0f}"
        if shares * fill > self.s["cash"] + 1e-6:
            return "not enough cash"
        return None

    def day_loss_hit(self, prices: dict[str, float] | None = None) -> bool:
        """True once today's loss reaches max_daily_loss_pct of the day's starting value (circuit breaker)."""
        pct = self.cfg.get("max_daily_loss_pct")
        if not pct:
            return False
        start = self.s.get("day_start_equity") or self.s["start_cash"]
        return self.equity(prices) <= start * (1 - pct / 100)

    def max_shares(self, price: float) -> int:
        fill = price * (1 + COST_PER_SIDE)
        cap = min(self.cfg["max_position_usd"], self.s["cash"])
        return max(0, math.floor(cap / fill))

    def buy(self, sym: str, price: float, shares: int, stop: float, target: float,
            why: str, now_ts: float, watchlist: list[str], setup: str = "") -> str | None:
        with self.lock:
            err = self.can_buy(sym, price, shares, stop, target, now_ts, watchlist)
            if err:
                return err
            fill = round(price * (1 + COST_PER_SIDE), 4)
            self.s["cash"] = round(self.s["cash"] - shares * fill, 4)
            self.s["positions"][sym] = {
                "shares": int(shares), "entry": fill, "entry_t": now_ts,
                "stop": round(stop, 4), "stop0": round(stop, 4), "target": round(target, 4),
                "why": why, "last": price, "setup": setup,
            }
            self.s["last_bar_t"][sym] = now_ts
            self.save()
            return None

    def sell(self, sym: str, price: float, now_ts: float, why: str) -> dict | None:
        with self.lock:
            p = self.s["positions"].pop(sym, None)
            if not p:
                return None
            fill = round(price * (1 - COST_PER_SIDE), 4)
            self.s["cash"] = round(self.s["cash"] + p["shares"] * fill, 4)
            tr = {
                "sym": sym, "shares": p["shares"], "entry": p["entry"], "exit": fill,
                "entry_t": p["entry_t"], "exit_t": now_ts,
                "pnl": round((fill - p["entry"]) * p["shares"], 2),
                "pnl_pct": round((fill / p["entry"] - 1) * 100, 3),
                "why_in": p["why"], "why_out": why, "setup": p.get("setup", ""),
            }
            self._append_trade(tr)
            self.save()
            return tr

    def adjust(self, sym: str, stop: float | None, target: float | None) -> str | None:
        """Move a stop (UP only) or a target. Returns a refusal reason or None."""
        with self.lock:
            p = self.s["positions"].get(sym)
            if not p:
                return f"no open {sym} position"
            last = p.get("last") or p["entry"]
            if stop is not None:
                if stop < p["stop"] - 1e-9:
                    return "stops only move up"
                if stop >= last:
                    return "stop must stay below the current price"
                p["stop"] = round(stop, 4)
            if target is not None:
                if target <= last:
                    return "target must stay above the current price"
                p["target"] = round(target, 4)
            self.save()
            return None

    def check_exits(self, sym: str, bars: list[dict], now: float | None = None) -> dict | None:
        """Treat the stop and target as resting orders. Walk the 1-minute bars that
        arrived since the last check, oldest first. A stop fills at the stop, or at
        the bar's open if price gapped through it; a target likewise. If one bar
        touches both, assume the stop (the cautious reading)."""
        with self.lock:
            p = self.s["positions"].get(sym)
            if not p:
                return None
            since = self.s["last_bar_t"].get(sym, p["entry_t"])
            for b in bars:
                if b["t"] <= since:
                    continue
                # 10-31 FIX (found by two independent code reviews, freqtrade + Lean/nautilus): Yahoo's newest bar is
                # usually the minute STILL FORMING. It used to be marked "checked" on first sight, so a stop touched
                # later in that same minute was never filled (3 of the first 10 trades exited late; net $5.32 kinder
                # than honest fills). Now a bar is only marked checked once it has finished (t + 60 <= now); the
                # forming bar is re-checked on every poll. now=None (tests, replays of finished days) = all complete.
                if now is None or b["t"] + BAR_SECONDS <= now:
                    self.s["last_bar_t"][sym] = b["t"]
                p["last"] = b["c"]
                if b["l"] <= p["stop"]:
                    px = min(p["stop"], b["o"])
                    return self.sell(sym, px, b["t"], f"stop hit {hm(b['t'])}")
                if b["h"] >= p["target"]:
                    px = max(p["target"], b["o"])
                    return self.sell(sym, px, b["t"], f"target hit {hm(b['t'])}")
            self.save()
            return None

    def new_day(self, day: str, prices: dict[str, float]) -> list[dict]:
        """Start of a new session. Anything still open (the program was off at the
        close) is sold at the latest price so nothing is ever carried overnight."""
        closed = []
        with self.lock:
            if self.s["day"] == day:
                return closed
            for sym in list(self.s["positions"]):
                px = prices.get(sym) or self.s["positions"][sym].get("last") or self.s["positions"][sym]["entry"]
                tr = self.sell(sym, px, et_now().timestamp(), "left open overnight (program was off at the close)")
                if tr:
                    closed.append(tr)
            self.s["day"] = day
            self.s["day_start_equity"] = self.equity()
            self.s["calls_today"] = 0
            self.save()
        return closed
