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

## Amendment A5 (Fri 2026-11-06 evening, after trade 12, live Mon 11-09): trade the open
tomahawkeer: "The busiest times of trading days are the first and last 30 min of the session. It's obvious why trades
are sparse if it won't trade before 10am." In practice the desk traded 10:00 to 3:30: the scanner started at 9:45 and a
rule the desk wrote for itself (R9) blocked buys before 10:00 on data days. That week gave 0, 1, 0, 1, 0 trades.
- **Playbook R9:** "no buys before 10:00 on data days" became "no buys within 2 minutes of a scheduled data release".
  (A playbook edit; the nightly review may change it again like any rule.)
- **Scanner start 9:45 → 9:35, `min_bars` 15 → 5.** Without the second change nothing could fire before 9:45 anyway.
  In practice only the flush-bounce trigger (and rarely breakout) can fire before 9:46; momentum needs 16 minutes of
  bars and VWAP reclaim 30. The first 5 minutes stay closed (widest spreads, least reliable prints on a free feed).
- **G2:** trades entered on or after 2026-11-09 draw their random copies from **9:35**; earlier trades keep 9:45, the
  first minute the desk could act on those days (`grade_read.entry_window(hold, day)`, pinned by tests).
- Unchanged: money limits, cost, no buys after 3:30, flat by 3:45, G1, the read points, A4, and the research grade
  (still 9:45 → 3:45, `pick_grade_start_minute`).
- Read 1 reports before/after 11-09 separately, plus the 9:35-10:00 trades on their own line (reported, not graded).
- Predictions on record: average trades/day rise above 1 over the first 5 days after it (~55%); the first ten
  9:35-10:00 trades are net negative (~65%).
