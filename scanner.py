"""The scanner: cheap checks every 30 seconds that decide WHEN to ask Claude.

No judgement lives here. A trigger only means "something is happening on this
stock, worth a look". Claude decides whether it is a trade. Every threshold is a
named setting in config.json so it can be tuned without touching code.
"""
from __future__ import annotations

from engine import minutes_et


def vwap(bars: list[dict]) -> float | None:
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
    vol = sum(b["v"] for b in bars)
    return pv / vol if vol > 0 else None


def to_5m(bars: list[dict]) -> list[dict]:
    """Group 1-minute bars into 5-minute bars (what Claude reads)."""
    out: list[dict] = []
    for b in bars:
        slot = b["t"] - (b["t"] % 300)
        if out and out[-1]["t"] == slot:
            o = out[-1]
            o["h"] = max(o["h"], b["h"]); o["l"] = min(o["l"], b["l"])
            o["c"] = b["c"]; o["v"] += b["v"]
        else:
            out.append({"t": slot, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    return out


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def scan(sym: str, bars: list[dict], qqq: list[dict], cfg: dict) -> list[tuple[str, str]]:
    """Return a list of (kind, plain-English reason) for this symbol right now."""
    s = cfg["scanner"]
    out: list[tuple[str, str]] = []
    if len(bars) < s["min_bars"]:
        return out
    last = bars[-1]
    m = minutes_et(last["t"])
    if m < s["start_minute"]:            # let the opening chaos settle first
        return out
    prior = bars[:-1]
    hod_prior = max(b["h"] for b in prior)
    lod = min(b["l"] for b in bars)
    vw = vwap(bars)

    # recent volume vs the day's typical 5-minute volume
    recent_v = sum(b["v"] for b in bars[-5:])
    avg5 = sum(b["v"] for b in bars) / len(bars) * 5
    vol_ratio = recent_v / avg5 if avg5 > 0 else 0.0

    # 1) breaking to a new high of day on rising volume
    if last["c"] > hod_prior and vol_ratio >= s["breakout_volume_ratio"]:
        out.append(("breakout", f"new high of day {last['c']:.2f} on {vol_ratio:.1f}x normal volume"))

    # 2) reclaiming VWAP after spending most of the last half hour below it
    if vw and len(bars) >= 30:
        below = sum(1 for b in bars[-30:-3] if b["c"] < vw)
        above_now = all(b["c"] > vw for b in bars[-3:])
        if below >= s["vwap_reclaim_bars_below"] and above_now:
            out.append(("vwap_reclaim", f"back above VWAP {vw:.2f} after {below} of 27 minutes below"))

    # 3) a flush that is bouncing: down a lot from the high, now up off the low
    i_hod = max(range(len(bars)), key=lambda i: bars[i]["h"])
    i_lod = min(range(len(bars)), key=lambda i: bars[i]["l"])
    drop = pct(lod, bars[i_hod]["h"])
    bounce = pct(last["c"], lod)
    fresh = (len(bars) - 1 - i_lod) <= s["flush_recent_minutes"]      # the low must be recent, not this morning's
    if fresh and i_lod > i_hod and drop <= -s["flush_drop_pct"] and bounce >= s["flush_bounce_pct"]:
        out.append(("flush_bounce", f"fell {abs(drop):.1f}% from the high, now {bounce:.1f}% off the low"))

    # 4) strong 15-minute push, stronger than the Nasdaq
    if len(bars) >= 16:
        mv = pct(last["c"], bars[-16]["c"])
        q = pct(qqq[-1]["c"], qqq[-16]["c"]) if len(qqq) >= 16 else 0.0
        if mv >= s["momentum_15m_pct"] and mv - q >= s["momentum_vs_qqq_pct"]:
            out.append(("momentum", f"up {mv:.1f}% in 15 minutes vs Nasdaq {q:+.1f}%"))
    return out


class Cooldowns:
    """Stops the same idea from being asked about over and over. Saved to disk, so a
    restart of RUN.bat does not make it re-ask everything it just asked."""

    def __init__(self, cfg: dict, path: str | None = None):
        import json, os
        self.cfg = cfg["scanner"]
        self.path = path
        self.last_any: dict[str, float] = {}
        self.last_kind: dict[tuple[str, str], float] = {}
        self.locks: dict[str, list] = {}            # sym -> [until_t, plain-English reason] (10-31: lock after a stop)
        if path and os.path.exists(path):
            try:
                d = json.load(open(path, encoding="utf-8"))
                self.last_any = d.get("any", {})
                self.last_kind = {tuple(k.split("|", 1)): v for k, v in d.get("kind", {}).items()}
                self.locks = d.get("locks", {})
            except Exception:
                pass

    def lock(self, sym: str, until: float, reason: str) -> None:
        """Hard lock (10-31, freqtrade's per-pair CooldownPeriod/StoplossGuard): no wake-ups about buying this stock
        until `until`. Set by the engine-side stop fill itself, not by asking Claude, so a prompt can't forget it."""
        cur = self.locks.get(sym)
        if not cur or until > cur[0]:
            self.locks[sym] = [until, reason]
        self._save()

    def locked(self, sym: str, now: float) -> str | None:
        l = self.locks.get(sym)
        return l[1] if l and now < l[0] else None

    def ready(self, sym: str, kind: str, now: float) -> bool:
        if self.locked(sym, now):
            return False
        if now - self.last_any.get(sym, float("-inf")) < self.cfg["cooldown_symbol_min"] * 60:
            return False
        if now - self.last_kind.get((sym, kind), float("-inf")) < self.cfg["cooldown_same_trigger_min"] * 60:
            return False
        return True

    def mark(self, sym: str, kind: str, now: float) -> None:
        self.last_any[sym] = now
        self.last_kind[(sym, kind)] = now
        self._save()

    def _save(self) -> None:
        if self.path:
            import json, os
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"any": self.last_any, "kind": {f"{k[0]}|{k[1]}": v for k, v in self.last_kind.items()},
                           "locks": self.locks}, f)
            os.replace(tmp, self.path)
