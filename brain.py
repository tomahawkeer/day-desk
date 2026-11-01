"""Asking Claude. Builds a compact picture of the moment, hands it to Claude Code
on this PC (your subscription, no API bill), and reads back one decision as JSON.

Claude's answer is a SUGGESTION: engine.py re-checks every limit before any
pretend money moves, so a bad or garbled answer can never break the rules.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading

from engine import et_now, hm
from scanner import to_5m, vwap, pct

SYSTEM = """You are the decision-maker for a PAPER day-trading desk (pretend money, no broker).
You trade US stocks LONG ONLY, intraday, flat by 3:45 PM ET. A cheap scanner woke you because something
is happening; most wake-ups should end in "pass". Only act on a concrete edge: relative strength vs the
Nasdaq, a breakout that is holding on volume, a flush that is reclaiming, or a news catalyst being bought.
Every buy needs a stop where the idea is proven wrong and a target where you would take profit, and a
reward at least about 1.5x the risk. Stops can only be raised, never lowered. Costs are tiny (0.01% per side).

Every buy is an experiment in a named STRATEGY ("setup"), so its result can be learned from. Reuse a name from
your scorecard when it is the same idea; give a genuinely new idea a new short name. Lean on setups your
scorecard and playbook say work; be wary of ones that keep losing; trying a new idea is fine if you name it.

Reply with ONE JSON object and nothing else:
{"action": "buy" | "pass" | "sell" | "hold" | "adjust",
 "setup": "<buys only: short strategy name, e.g. vwap-reclaim, breakout-hold, flush-reclaim, news-catalyst>",
 "shares": <int, buys only; at most the max shown>,
 "stop": <price>, "target": <price>,
 "rules": [<ids of YOUR playbook rules this decision follows or applies, e.g. "R3"; [] if none>],
 "p_win": <optional, 0-100: your honest chance the trade reaches its target before its stop - for a pass, if you had bought now with sensible levels>,
 "why": "<one or two plain-English sentences a non-trader understands>"}
Use "buy"/"pass" when you are asked about a stock you do not hold, and "hold"/"sell"/"adjust" for one you hold
("adjust" = move the stop up and/or the target)."""


def _fmt_bars(bars: list[dict]) -> str:
    lines = ["time_ET,open,high,low,close,volume"]
    for b in bars:
        lines.append(f"{hm(b['t'])},{b['o']:.2f},{b['h']:.2f},{b['l']:.2f},{b['c']:.2f},{int(b['v'])}")
    return "\n".join(lines)


def fmt_scorecard(rows: list[dict] | None) -> str:
    if not rows:
        return "(no closed trades yet)"
    return "\n".join(f"- {r['setup']}: {r['trades']} trades over {r['days']} day(s), {r['win_rate']}% wins, "
                     f"total {r['pnl']:+.2f}, average {r['avg_pct']:+.2f}% per trade" for r in rows)


def fmt_playbook(lessons: list[dict] | None) -> str:
    if not lessons:
        return "(empty so far: no trading days reviewed yet)"
    return "\n".join(f"- {l['id'] + ' ' if l.get('id') else ''}[{l.get('status', 'tentative')}] {l.get('rule', '')}"
                     for l in lessons)


def build_prompt(sym: str, reason: str, bars: list[dict], qqq: list[dict], news: list[str],
                 book_view: dict, max_shares: int, lessons: list[dict] | None = None,
                 scores: list[dict] | None = None, research: dict | None = None, earlier_today: str = "",
                 base_rates: str = "") -> str:
    last = bars[-1]["c"]
    # Every number below is a "fact" Claude trusts. Audit 10-31 (claude-trading-desk bug list): never print a made-up
    # value when the data can't support it - say "n/a" and why. Before this, zero volume made VWAP None and the
    # f-string raised (the call was counted, never logged, and the rest of that tick's asks were lost); fewer than
    # 16 QQQ bars printed "+0.00% in the last 15 minutes"; a zero open printed "+0.00% since the open"; no measurable
    # range printed "0.00%" as the noise yardstick.
    # Noise yardstick (10-31): the stock's typical 1-minute high-low range today, so a stop can be judged in units
    # of normal wiggle instead of guessed. The trade lab measures every past stop the same way.
    rng = [(b["h"] - b["l"]) / b["o"] * 100 for b in bars if b["o"]]
    noise = sum(rng) / len(rng) if rng else 0.0
    day_open = bars[0]["o"]
    vw = vwap(bars)
    since_open = f"{pct(last, day_open):+.2f}% since the open" if day_open else "change since the open n/a (no open price)"
    vw_txt = f"VWAP {vw:.2f}" if vw else "VWAP n/a (no volume recorded yet)"
    noise_line = (f"Typical 1-minute range today: {noise:.2f}% (a stop {noise * 3:.2f}% away is about 3 normal one-minute "
                  f"wiggles)." if noise > 0 else "Typical 1-minute range today: n/a (no measurable range yet).")
    q_line = "Nasdaq (QQQ): no bars available right now."
    if qqq:
        q_open = qqq[0]["o"]
        q_since = f"{pct(qqq[-1]['c'], q_open):+.2f}% since the open" if q_open else "change since the open n/a"
        q_15 = (f"{pct(qqq[-1]['c'], qqq[-16]['c']):+.2f}% in the last 15 minutes" if len(qqq) >= 16 and qqq[-16]["c"]
                else f"last-15-minute change n/a (only {len(qqq)} minute(s) of QQQ bars so far)")
        q_line = f"Nasdaq (QQQ): {qqq[-1]['c']:.2f}, {q_since}, {q_15}."
    held = book_view["positions"].get(sym)
    pos_line = "You do NOT hold it."
    if held:
        pos_line = (f"You HOLD {held['shares']} shares bought {hm(held['entry_t'])} at {held['entry']:.2f} "
                    f"(strategy: {held.get('setup') or 'untagged'}); "
                    f"stop {held['stop']:.2f}, target {held['target']:.2f}. Your reason then: {held['why']}")
    others = ", ".join(f"{s} ({p['shares']} sh)" for s, p in book_view["positions"].items() if s != sym) or "none"
    return f"""Time now: {et_now().strftime('%a %b %d')} {hm(bars[-1]['t'])} ET.
Stock: {sym}. Why you were woken: {reason}.
{sym} last {last:.2f}, {since_open}, high {max(b['h'] for b in bars):.2f}, low {min(b['l'] for b in bars):.2f}, {vw_txt}.
{noise_line}
{earlier_today}
{base_rates}
{q_line}
{pos_line}
Account: cash ${book_view['cash']:,.2f}; other open positions: {others}; max shares you may buy now: {max_shares}.

Today's 5-minute bars:
{_fmt_bars(to_5m(bars))}

Last 30 one-minute bars:
{_fmt_bars(bars[-30:])}

Headlines:
{chr(10).join(news) if news else '(none found)'}
{fmt_research_for(sym, research)}

Your scorecard by strategy (all closed trades so far):
{fmt_scorecard(scores)}

Your playbook (lessons YOU wrote after reviewing your own past trading days; "solid" ones have repeated evidence,
"tentative" ones rest on one or two examples, so weigh them accordingly):
{fmt_playbook(lessons)}
"""


REVIEW_SYSTEM = """You are reviewing your OWN paper day-trading for today, to trade better tomorrow.
You see every trade you made (with how far it went for and against you while open) and every time you were
woken and passed or held (with what the stock did in the 60 minutes after). Judge your calls by the evidence,
not by hindsight luck: one lucky or unlucky trade proves little.

Then rewrite your playbook: at most 15 short, concrete, checkable rules for future decisions (for example
"after a flush, wait for a 5-minute close back above VWAP before buying" - not "be careful").
- Keep rules that today's evidence supports, sharpen vague ones, merge duplicates.
- Drop or rewrite rules that today's evidence contradicts.
- Add a rule only from a real pattern in the data. status "tentative" = 1-2 examples; "solid" = 3+ examples
  across at least 2 different days. Never mark something solid from a single day. The code CHECKS this: "solid" is
  only kept when the rule-credit table below shows at least 3 closed trades on 2+ days that cited it.
- Every rule keeps its id. Give the SAME id ("R3") to a rule you keep or sharpen, "new" to a genuinely new rule.
  The rule-credit table (decisions cite rule ids) is how a rule earns or loses trust over days.
- Rules about what WORKED count as much as rules about mistakes. Missed good trades (passes that ran) are mistakes too.
- Rules must fit the fixed limits (long only, flat by 3:45 PM, stop at most 3% away, max 4 positions, $2,500 each,
  no new buys after the account is down 2% on the day).

Also grade every STRATEGY (setup) on the scorecard and any you used today: "keep" (working), "adjust" (say exactly
what to change), "drop" (stop using it), or "needs more data" (too few trades to tell; under ~10 trades is usually
this). Say what that strategy taught you, citing its numbers. Do not call a strategy proven from a handful of trades.

Reply with ONE JSON object and nothing else:
{"summary": "<3-5 plain-English sentences on how today went and what you learned, for tomahawkeer, who is not a trader>",
 "strategies": [{"setup": "<name>", "verdict": "keep" | "adjust" | "drop" | "needs more data",
                 "note": "<what it taught you and, for adjust, the exact change>"}],
 "lessons": [{"id": "<R-number you are keeping, or new>", "rule": "<the rule>",
              "evidence": "<which trades/passes and dates support it>", "status": "tentative" | "solid"}]}"""


def _move_after(bars: list[dict], t: float, price: float, minutes: int = 60) -> str:
    after = [b for b in bars if t < b["t"] <= t + minutes * 60]
    if not after or not price:
        return "no data after"
    hi = max(b["h"] for b in after); lo = min(b["l"] for b in after); end = after[-1]["c"]
    return (f"next {len(after)} min: best {pct(hi, price):+.2f}%, worst {pct(lo, price):+.2f}%, "
            f"ended {pct(end, price):+.2f}%")


def build_review_prompt(day: str, trades: list[dict], decisions: list[dict],
                        bars: dict[str, list[dict]], lessons: list[dict] | None,
                        scores: list[dict] | None = None, verdicts: list[dict] | None = None) -> str:
    tl = []
    for t in trades:
        held = [b for b in bars.get(t["sym"], []) if t["entry_t"] <= b["t"] <= t["exit_t"]]
        swing = ""
        if held:
            swing = (f"; while open it went as high as {pct(max(b['h'] for b in held), t['entry']):+.2f}% "
                     f"and as low as {pct(min(b['l'] for b in held), t['entry']):+.2f}%")
        tl.append(f"- [{t.get('setup') or 'untagged'}] {t['sym']} {t['shares']} sh, in {hm(t['entry_t'])} @ {t['entry']:.2f}, out {hm(t['exit_t'])} "
                  f"@ {t['exit']:.2f}, P&L {t['pnl']:+.2f} ({t['pnl_pct']:+.2f}%){swing}. "
                  f"Why in: {t['why_in']} | Why out: {t['why_out']}")
    dl = []
    for d in decisions:
        if d.get("action") in ("pass", "hold", "buy", "sell", "adjust") and d.get("trigger") != "end-of-day review":
            after = _move_after(bars.get(d["sym"], []), d["t"], d.get("price") or 0)
            dl.append(f"- {hm(d['t'])} {d['sym']} woken by {d.get('trigger')} ({d.get('reason', '')}); "
                      f"you said {d['action'].upper()}: {d.get('why', '')} -> {d.get('result', '')}. Afterwards, {after}.")
    return f"""Trading day: {day}.

Trades closed today ({len(trades)}):
{chr(10).join(tl) if tl else '(none)'}

Every time you were asked today ({len(dl)}):
{chr(10).join(dl) if dl else '(none)'}

Scorecard by strategy, all days so far (computed from the trade log):
{fmt_scorecard(scores)}

Your strategy verdicts going into today:
{chr(10).join(f"- {v['setup']}: {v['verdict']} - {v.get('note', '')}" for v in (verdicts or [])) or '(none yet)'}

Your playbook going into today:
{fmt_playbook(lessons)}
"""


def parse_review(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in the review")
    d = json.loads(m.group(0))
    lessons = []
    for l in d.get("lessons", [])[:15]:
        rule = str(l.get("rule", "")).strip()
        if rule:
            st = str(l.get("status", "tentative")).lower()
            rid = str(l.get("id", "")).strip().upper()
            lessons.append({"id": rid if re.match(r"^R\d{1,3}$", rid) else None,
                            "rule": rule[:400], "evidence": str(l.get("evidence", ""))[:400],
                            "status": st if st in ("tentative", "solid") else "tentative"})
    strategies = []
    for s in d.get("strategies", [])[:30]:
        name = str(s.get("setup", "")).strip()
        if name:
            v = str(s.get("verdict", "needs more data")).lower()
            strategies.append({"setup": name[:60], "note": str(s.get("note", ""))[:500],
                               "verdict": v if v in ("keep", "adjust", "drop", "needs more data") else "needs more data"})
    return {"summary": str(d.get("summary", "")).strip()[:1500], "lessons": lessons, "strategies": strategies}


RESEARCH_SYSTEM = """You are the pre-market researcher for a PAPER day-trading desk (long only, intraday, flat by
3:45 PM ET, liquid US stocks and ETFs). The prompt names the session you are researching for: usually this morning,
before the open; on a weekend or evening it is a PREVIEW of the next session (then "today" below means that session,
and the search is for news since the last close and events scheduled before and during that session). Use web search
to find the 3 to 6 stocks most worth WATCHING that session for a possible long day trade, and say plainly why.
Always search: never answer from memory, and never skip the search because the market is closed right now.

Look for: pre-market movers with a real, fresh catalyst (earnings, guidance, upgrades/downgrades, FDA, contracts,
M&A, big product news), unusual pre-market volume, and sector or theme momentum. Check the backdrop too: index
futures, and scheduled events today with their Eastern times (economic data, Fed speakers, big earnings before/after
the bell). Skip: stocks under $5, thinly traded names (roughly under 1 million shares a day), and anything with no
identifiable catalyst. A stock gapping DOWN on bad news can still be worth watching for a long bounce, but say so.
Prefer quality of reasoning over quantity; cite where each catalyst came from. Keep it to about 15 searches.
If the US stock market is closed on the session date (a holiday), say so and return no picks.

Reply with ONE JSON object and nothing else:
{"backdrop": "<2-4 plain-English sentences: futures, mood, today's scheduled events with ET times>",
 "picks": [{"ticker": "<US ticker>", "catalyst": "<what happened, 1-2 sentences>",
            "why_watch": "<why it could set up a long day trade>", "setup": "<what would make it a buy, e.g.
            holds above pre-market high after 9:45 on volume>", "avoid_if": "<what would rule it out>",
            "levels": "<pre-market high/low, prior close or other levels if known, else empty>",
            "source": "<site or outlet names>",
            "conviction": "high" | "medium" | "low"}]}

"conviction" decides what happens next: "high" picks are ADDED TO THE TRADABLE WATCHLIST AUTOMATICALLY, so be stingy.
"high" only for a stock that looks prime for a profitable long day trade TODAY: a fresh, real catalyst, heavy volume,
a liquid large or mid cap, the move still in play (not exhausted), and a clean setup you could act on. Most days that is
0-2 stocks, sometimes none. Bounce-only ideas, thin names, and stories without a clear setup are "medium" at best."""


def parse_research(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in the research")
    d = json.loads(m.group(0))
    picks, seen = [], set()
    for p in d.get("picks", [])[:8]:
        t = str(p.get("ticker", "")).strip().upper().replace("$", "")
        if not re.match(r"^[A-Z]{1,5}([.][A-Z])?$", t) or t in seen:
            continue
        seen.add(t)
        conv = str(p.get("conviction", "medium")).strip().lower()
        picks.append({"ticker": t, **{k: str(p.get(k, "")).strip()[:500] for k in
                      ("catalyst", "why_watch", "setup", "avoid_if", "levels", "source")},
                      "conviction": conv if conv in ("high", "medium", "low") else "medium"})
    return {"backdrop": str(d.get("backdrop", "")).strip()[:1200], "picks": picks}


def fmt_research_for(sym: str, research: dict | None) -> str:
    if not research:
        return ""
    lines = []
    if research.get("backdrop"):
        lines.append(f"This morning's market backdrop (your pre-market research): {research['backdrop']}")
    for p in research.get("picks", []):
        if p["ticker"] == sym:
            lines.append(f"This morning you flagged {sym}: catalyst: {p['catalyst']} Why watch: {p['why_watch']} "
                         f"Buy setup you named: {p['setup']} Avoid if: {p['avoid_if']} Levels: {p['levels']}")
    return "\n".join(lines)


def to_price(v) -> float | None:
    """Read a price the way a person would write it (10-31, idea from TradingAgents agents/schemas.py:33-55):
    "$1,234.50" -> 1234.5; None, "", "N/A", "none", "-" -> None; a percent like "15%" -> None (a percent is never a
    price). Before this, one "N/A" stop on a pass threw the whole decision away as an error. A buy left with no stop
    is still refused by engine.can_buy, so this changes nothing about safety."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace("$", "").replace(",", "")
    if not t or t.lower() in ("n/a", "na", "none", "null", "-", "—") or t.endswith("%"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_decision(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in the reply")
    d = json.loads(m.group(0))
    act = str(d.get("action", "")).lower()
    if act not in {"buy", "pass", "sell", "hold", "adjust"}:
        raise ValueError(f"unknown action {act!r}")
    d["action"] = act
    for k in ("stop", "target"):
        d[k] = to_price(d.get(k))
    sh = to_price(d.get("shares"))
    d["shares"] = int(sh) if sh is not None else None
    d["why"] = str(d.get("why", "")).strip()[:600]
    rules = d.get("rules") if isinstance(d.get("rules"), list) else []
    d["rules"] = [str(r).strip().upper() for r in rules if re.match(r"^R\d{1,3}$", str(r).strip(), re.I)][:6]
    pw = d.get("p_win")                   # optional (10-31, decision_score.py calibration): int 0-100 or None
    pw = to_price(pw.strip().rstrip("%") if isinstance(pw, str) else pw)       # "65%" -> 65; "N/A" -> None
    d["p_win"] = int(round(min(100.0, max(0.0, pw)))) if pw is not None and pw == pw else None
    d["setup"] = re.sub(r"[^a-z0-9\- ]", "", str(d.get("setup") or "").lower()).strip().replace(" ", "-")[:40]
    return d


WINDOW_TITLE = "Day Desk (paper trading)"


def set_window_title(title: str = WINDOW_TITLE) -> None:
    """Name the console window (Windows only; does nothing elsewhere)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def find_claude_cli() -> str | None:
    """Path to the Claude Code command on this PC, or None if it is not installed."""
    import shutil
    for name in ("claude", "claude.cmd", "claude.exe"):
        p = shutil.which(name)
        if p:
            return p
    home = os.path.expanduser("~")
    for p in (os.path.join(home, ".local", "bin", "claude.exe"),
              os.path.join(home, "AppData", "Roaming", "npm", "claude.cmd")):
        if os.path.exists(p):
            return p
    return None


# WHERE "claude -p" RUNS (2026-10-31). Claude Code reads every CLAUDE.md in the folder it starts in AND in every
# folder above it, and adds them to the prompt. If it started in this program's folder it would read Day Desk's own
# CLAUDE.md on every call, and once Day Desk moved inside %USERPROFILE%\daytrading-bot (tomahawkeer, 10-31) it would also
# read the closed bot's 13 KB working agreement: thousands of wasted tokens on up to max_calls_per_day calls, plus
# rules about a different program leaking into trading decisions. So every call starts in an EMPTY folder under the
# system temp directory, which has no project above it. The prompt we send already carries everything a call needs.
CLAUDE_WORKDIR_NAME = "day-desk-claude-calls"


def claude_workdir() -> str:
    d = os.path.join(tempfile.gettempdir(), CLAUDE_WORKDIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def unwrap_cli_json(out: str) -> tuple[str, dict | None]:
    """Claude Code's --output-format json prints one object: the answer in "result" plus usage and cost.
    Returns (answer text, compact usage). If the output isn't that object (older CLI, odd output) the raw text
    is returned unchanged with usage None, so a format surprise can never break trading."""
    try:
        o = json.loads(out)
    except (ValueError, TypeError):
        return out, None
    if not isinstance(o, dict) or o.get("type") != "result":
        return out, None
    if o.get("is_error") or "result" not in o:
        raise RuntimeError(f"Claude Code reported an error: {o.get('subtype') or ''} {str(o.get('result') or '')}"[:300])
    u = o.get("usage") or {}
    tin, tcw, tcr = (int(u.get(k) or 0) for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    usage = {"tok_in": tin + tcw + tcr, "tok_new": tin + tcw, "tok_cached": tcr,
             "tok_out": int(u.get("output_tokens") or 0),
             "cost_usd": round(float(o.get("total_cost_usd") or 0), 5),
             "secs": round((o.get("duration_ms") or 0) / 1000, 1), "turns": o.get("num_turns"),
             # NOT reliable for Claude Code's own WebSearch tool: a 10-31 research run that clearly searched (20 turns,
             # sourced picks) reported 0 here. Use "turns" > 1 as the did-it-use-tools signal.
             "searches": int(((u.get("server_tool_use") or {}).get("web_search_requests")) or 0)}
    return str(o["result"]), usage


class ClaudeCLIBrain:
    """Asks Claude through Claude Code on this PC ("claude -p"), which runs on tomahawkeer's
    Claude SUBSCRIPTION login. There is no API key anywhere in this program, and any
    ANTHROPIC_API_KEY in the environment is REMOVED before the call, because if that
    variable were set Claude Code would bill the API account instead of the plan."""

    def __init__(self, exe: str, model: str, timeout_s: int = 150, lean: dict | None = None):
        self.exe, self.model, self.timeout_s = exe, model, timeout_s
        # LEAN MODE (10-31, config "claude_lean"). CORRECTED 10-31 evening: the bloat came from `--tools ""` (below),
        # which also removes ToolSearch, so all MCP/connector tools load upfront; never use --tools "" without
        # --strict-mcp-config. A one-line health check measured 429,398 input tokens on tomahawkeer's
        # PC: Claude Code was loading its whole interactive context (connectors/MCP servers, skills, memory files,
        # its coding system prompt) into every desk call. --bare would fix it but needs an API key, which tomahawkeer's rule
        # forbids, so the lean switches below keep the subscription login and strip the rest. TOKEN_PROBE.bat
        # measured each one before it was turned on.
        self.lean = lean or {}
        self._tl = threading.local()          # each thread's last call usage (research runs beside trading)

    def last_usage(self) -> dict | None:
        """What the most recent call ON THIS THREAD cost: tokens and the API-equivalent dollars Claude Code
        reports. tomahawkeer pays nothing extra (subscription); the dollars are just the best single yardstick of how
        much plan usage a call ate. None if the CLI didn't report it."""
        return getattr(self._tl, "usage", None)

    def _run(self, system: str, prompt: str, timeout_s: int | None = None, tools: str = "",
             model: str | None = None, max_turns: int | None = None) -> str:
        import subprocess
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"     # don't rename the Day Desk window
        # Run Claude Code without attaching it to this window (Windows: CREATE_NO_WINDOW), so it can't
        # retitle it or flash a console; stdin/stdout are pipes so nothing is lost.
        flags = 0x08000000 if os.name == "nt" else 0
        tail = "Do not use any tools." if not tools else "Use only web search/fetch; touch no files."
        lean = self.lean
        env.update({k: str(v) for k, v in (lean.get("env") or {}).items()})
        sys_file = None
        if lean.get("replace_system_prompt") and not tools:
            # Trading/review/ping calls: the desk's own instructions REPLACE Claude Code's coding-assistant prompt.
            # Research (web tools) keeps the default prompt, which explains how its tools work.
            sys_file = os.path.join(claude_workdir(), f"system-{os.getpid()}-{threading.get_ident()}.txt")
            with open(sys_file, "w", encoding="utf-8") as f:
                f.write(system)
            full = prompt + "\n\nAnswer with the JSON object only. " + tail
        else:
            full = system + "\n\n---\n\n" + prompt + "\n\nAnswer with the JSON object only. " + tail
        # --tools "" = no tools at all for trading calls (fast, light on usage); research gets web search only.
        # JSON output (10-31): same answer text in "result", plus the call's token usage and cost, so every
        # call's share of tomahawkeer's plan is logged instead of guessed from weekly % readings.
        args = [self.exe, "-p", "--output-format", "json", "--model", model or self.model, "--tools", tools]
        if tools:
            args += ["--allowedTools"] + tools.split(",")
        args += [str(a) for a in (lean.get("args") or [])]
        if sys_file:
            args += ["--system-prompt-file", sys_file]
        if max_turns:
            args += ["--max-turns", str(max_turns)]
        r = subprocess.run(args,
                           input=full, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout_s or self.timeout_s, env=env, creationflags=flags,
                           cwd=claude_workdir())
        set_window_title()                                    # belt and braces
        self._tl.usage = None
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or f"exit code {r.returncode}").strip()[:300])
        text, usage = unwrap_cli_json(r.stdout)
        self._tl.usage = usage
        return text

    def ping(self) -> str:
        """Self-test (mailbox 'claude'): the smallest possible call, to prove Claude Code still runs on the plan."""
        return self._run("You are a health check for a paper-trading program.",
                         'Reply with exactly this JSON object: {"ok": true, "says": "DAY DESK OK"}', timeout_s=120)

    def decide(self, prompt: str) -> tuple[dict, str]:
        out = self._run(SYSTEM, prompt)
        return parse_decision(out), out

    def review(self, prompt: str) -> dict:
        return parse_review(self._run(REVIEW_SYSTEM, prompt, timeout_s=360))   # a longer think, once a day

    def research(self, prompt: str, model: str | None = None) -> dict:
        return parse_research(self._run(RESEARCH_SYSTEM, prompt, timeout_s=900, tools="WebSearch,WebFetch",
                                        model=model, max_turns=40))


class FakeBrain:
    """For tests: always answers with what it was given."""

    def __init__(self, answers: list[dict] | None = None, review_answer: dict | None = None):
        self.answers = list(answers or [])
        self.prompts: list[str] = []
        self.review_prompts: list[str] = []
        self.review_answer = review_answer or {"summary": "test day", "lessons": [
            {"rule": "test rule", "evidence": "test", "status": "tentative"}]}

    def review(self, prompt: str) -> dict:
        self.review_prompts.append(prompt)
        return parse_review(json.dumps(self.review_answer))

    research_answer = {"backdrop": "Futures flat.", "picks": [
        {"ticker": "PLTR", "catalyst": "beat earnings", "why_watch": "gap up on volume", "setup": "holds premarket high",
         "avoid_if": "fades below VWAP", "levels": "PMH 190", "source": "Reuters", "conviction": "high"},
        {"ticker": "ENPH", "catalyst": "cut outlook", "why_watch": "bounce only", "setup": "reclaims VWAP",
         "avoid_if": "new lows", "levels": "", "source": "CNBC", "conviction": "medium"}]}
    research_prompts: list = []

    def research(self, prompt: str, model: str | None = None) -> dict:
        self.research_prompts.append(prompt)
        return parse_research(json.dumps(self.research_answer))

    ping_reply = '{"ok": true, "says": "DAY DESK OK"}'

    def ping(self) -> str:
        if isinstance(self.ping_reply, Exception):
            raise self.ping_reply
        return self.ping_reply

    def decide(self, prompt: str):
        self.prompts.append(prompt)
        d = self.answers.pop(0) if self.answers else {"action": "pass", "why": "test default"}
        return parse_decision(json.dumps(d)), json.dumps(d)
