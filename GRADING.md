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
