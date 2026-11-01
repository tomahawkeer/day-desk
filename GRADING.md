# How Day Desk is graded (pre-registered)

The grading rules were written down **before** the trades they grade, and they don't change after results are
seen. `grade_read.py` (run with `GRADE.bat`) implements them exactly.

## When
By trade count, not by date: **Read 1 at 60 closed trades, Read 2 at 120.** Between reads there are running
numbers but no verdict, no "it's working", and no increase in size.

## The two tests (both must pass)
- **G1 — Profitable:** total net P&L of every closed trade > $0 after a 0.01%-per-side cost.
- **G2 — Beats random timing:** 2,000 copies of the whole record. In each copy every real trade is replaced by the
  same stock, same day, same share count and same holding minutes, but with the entry minute drawn at random from the
  minutes the desk could have bought in. G2 passes if the real total beats at least 95% of the copies.

PASS = G1 and G2. Anything else = no evidence of skill, reported as "x of 2" and never re-stated with one dropped.

## Reported beside every verdict
- **Resolution floor:** the copies' 95th percentile minus their median, i.e. the edge the record would need to clear
  G2. A FAIL with a large floor means "an edge this small can't be seen yet", not "no edge".
- **Known weakness of the control:** copies exit on time while real trades mostly exit on stops, so the read also
  reports copies with each trade's own stop distance applied (reported, not graded).
- Stress cost at 0.05%/side, QQQ and SPY over the same days, win rate, P&L by setup and by stock, worst drawdown,
  first half vs second half of the record.

## No rescue by slicing
If Read 2 is not a PASS, a profitable subset (one setup, one stock, the second half) is a hypothesis for a **new**
pre-registration, never the verdict.

## Changes
Changing the prompt, playbook, scanner or learning loop is part of the system being tested. Changing a money limit
(position size, stop cap, daily loss breaker, max positions) or the cost model is an **amendment**: dated, reasoned,
written down before it takes effect, outside market hours. `engine.py` pins the money limits and the desk pauses
itself if they change.

## Amendment A3 (Sat 2026-10-31, after trade 10, outside market hours): fill model
- A stop or target touched late in a still-forming minute used to be missed, because that minute was marked "checked"
  the first time it was seen. Now a minute counts as checked only once it has finished. On trades 1 to 10 this changed
  3 trades and made the record **$5.32 worse**: the recorded fills had been kinder than honest ones.
- Buys and Claude's sells fill at the price fetched **after** Claude answers, not before the 5 to 150 second call. A buy
  is refused if that price is stale or already through the stop.
- The scanner and Claude see finished minutes only.
- Nothing else changed (cost, limits, times, the grade, the read points). Trades 1 to 10 stay as recorded, and Read 1
  reports G1 both as recorded and re-filled on finished bars.
- Grader ruling: random entries are drawn from 9:45 to min(15:30, 15:45 minus the hold), so every copy closes by 3:45.
  The 95th percentile is nearest-rank, and "beats" means strictly greater.

## Addendum A4 (Sat 2026-10-31 evening, before Mon 11-02): a control arm, reported only
Does Claude's filtering beat a dumb rule on the **same** candidates? Candidates are every Claude decision (buy or pass)
on a stock not held, from a scanner trigger, from 11-02 on. Three arms:
- **ALL:** take every candidate.
- **GATED:** take it only if, on the last finished minute, the close is above session VWAP **and** the stock's % move
  since the open beats QQQ's over the same bars. Fixed here before any data; never tuned.
- **CLAUDE:** what Claude said buy to.

Every arm is scored with the same neutral trade: stop 2 typical 1-minute ranges below (max 3%), target 1.5x risk, out
by 3:45, 0.01% per side. Reported: n, mean R, target-first %, a 90% interval bootstrapped over days, CLAUDE minus GATED
with its interval, and the overlap. It never changes PASS/FAIL. Prediction on record: CLAUDE minus GATED straddles 0
at Read 1 (~75%).
