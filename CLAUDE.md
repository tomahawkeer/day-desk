# Working agreement — day-desk

tomahawkeer cannot code. He runs .bat files and follows clear steps. Deliver complete runnable files, explain in plain
English, and swear casually if it fits. PAPER ONLY: there is no broker code here, and none gets added without tomahawkeer
saying so explicitly.

## What this is
A desktop paper day-trader (built 2026-10-28). It polls Yahoo 1-minute bars every 30 seconds for tomahawkeer's watchlist
(`data/watchlist.json`, which he edits from the phone page). A scanner (`scanner.py`) decides when to wake Claude
through Claude Code on the PC (`claude -p`, tomahawkeer's SUBSCRIPTION; `brain.py` strips ANTHROPIC_API_KEY from the call's environment; daily call cap in `config.json`). Claude's JSON answer is only a
suggestion: `engine.py` enforces every money rule. The phone page (`page.html`) is served by `day_desk.py` on port 8790,
reached over Tailscale with NO access key (tomahawkeer 10-28; `require_access_key: false`). This is the ONLY Day Desk: the
cloud hourly test (artifact HqpTt6Baf5gAzNh8LuH5G3) was stopped 10-28. Handoff and ledger live in the claude.ai Project
"Daytrading Bot": start at `claude/memory/memory_index.md` (it names the newest handoff), then
`claude/memory/day-desk-ledger.md`. Grading rules: `claude/day-desk-preregistration.md` PART 2.

**Where it lives (10-31):** `%USERPROFILE%\daytrading-bot\day-desk` — moved inside the project folder so every
session gets it without a separate folder grant (before 10-31: `%USERPROFILE%\day-desk`). Every launcher is
relative (`%~dp0`), so the move changed nothing in how it runs. **`claude -p` never runs in this folder**: brain.py
starts every call in an empty temp folder (`claude_workdir()`), because Claude Code reads every CLAUDE.md from its start
folder upward and would otherwise feed this file AND the closed bot's `daytrading-bot\CLAUDE.md` into each trading call.
A test pins it.

## Rules
1. **Limits live in engine.py, never only in the prompt.** Watchlist membership, 4 positions, $2,500, stop at most 3%
   away, no buys at or after 3:30 PM, flat at 3:45 PM, stops only move up, no new buys once the day is down
   `max_daily_loss_pct` (2%, Claude's call 10-29 after tomahawkeer handed it money management). The tests pin every one; never weaken a test.
2. **Stops and targets are resting orders.** Walk the new 1-minute bars oldest first; gap → fill at the open; both in
   one bar → stop.
3. **Every threshold is a named, commented setting in config.json.** No magic numbers in logic.
4. **Every Claude call is logged** in `data/decisions.jsonl` with the trigger, the answer and the result, including
   refusals and errors.
5. Run `TEST.bat` after any change (219 tests as of the 10-31 weekend build). Run `REHEARSAL.bat` too (rule 23).
6. yfinance is unofficial: any feed failure must show on the page (red pill) and be retried, never be silent.
7. **Never add an API key or any separately billed service.** tomahawkeer (10-28): "im not getting charged additionally and separately".
8. **It learns from its own record (tomahawkeer 10-28: "if you use a strategy to test and it works or doesn't work, you record the
   result and try to see what you learned from it").** Every buy carries a `setup` tag; `engine.scorecard()` scores
   setups from `data/trades.csv`; one end-of-day `claude -p` review (after the 3:45 close-out) grades each setup and
   rewrites `data/lessons.json` (≤15 rules, tentative/solid; history in `lessons_history.jsonl`); every decision prompt
   carries the scorecard and playbook. Never let a handful of trades be called proof.
9. **Morning research (tomahawkeer 10-28: "an agent that researches stock news every trading morning to find a few stocks worth
   watching").** Weekdays at `research_minute` (8:45 ET), once per day, in a background thread: `claude -p` with
   `--tools "WebSearch,WebFetch"` only → `data/picks.json` (+ history). Each pick has a conviction. **tomahawkeer 10-28: "You have
   permission if any pick ever looks to be prime to be profitable for that day"** → `auto_add_picks: "high"` adds HIGH
   picks to the watchlist for that day (`data/auto_added.json`); they come off next morning unless held or re-picked;
   tomahawkeer's own tickers are never removed. Other picks keep the Add button. Trading/review calls run with `--tools ""`.
   **tomahawkeer 10-30: every pick is priced WATCH-ONLY** (`price_all_picks`, cap `max_watch_only_picks`): fetched with the
   watchlist, never scanned/asked/bought; at the 3:45 close-out each pick's 9:45→3:45 move vs QQQ is appended to
   `data/pick_results.jsonl` (once a day, `picks_scored_day` in state). That file is how the research gets graded.
   **Bar archive (10-30):** at the close-out every 1-min bar of the day goes to `data/bars/<day>.json` (once a day,
   `bars_saved_day`). The pre-registered random-timing grade needs it; Yahoo keeps 1-min bars only ~30 days.
10. Every JSON data file is written atomically (`write_json`: temp file + os.replace); the research thread runs beside
   the trading loop, and a half-written file once made a reader crash (flaky test, 10-28).
11. **Ship to this folder from a freshly named staged path and re-check every byte size** (the copy tool has reported
   success without writing). **Click-test page changes in headless Chromium.** Python/config changes need tomahawkeer to
   restart RUN.bat, which he can only do after work; page.html is served fresh.
12. **Proof of life (10-29, `health.py`).** A thread rewrites `data/heartbeat.json` every 30 s (`verdict` says
   WORKING / LOOP STUCK / FEED FAILING / STARTING). File age = is the program alive; `loop_age_s` = is the trading loop
   alive. Mailbox: drop a NEW-named file holding `ping`, `feed`, `claude` or `help` into `data/inbox/`; the answer lands
   in `data/outbox/<name>.json` in ~10 s (`feed` waits for the next loop pass, up to 60 s off-hours). `feed` runs inside
   the trading loop because two yfinance downloads at once are unsafe. `claude` is capped at 5/day, logged in
   decisions.jsonl as trigger `self-test`, not counted against the trading cap, and kept out of the end-of-day review.
   `write_json` retries the swap for 2 s: Windows refuses os.replace while a reader has the file open (WinError 5,
   found by TEST.bat 10-29, invisible on Linux). Browser: `/api/ping`. From the cloud: `device_list_dir data` and read heartbeat.json's mtime, no staging needed.
13. **Usage is measured, per call (10-31).** `claude -p --output-format json`; `brain.unwrap_cli_json` pulls the answer
   out of `result` and keeps tokens + Claude Code's API-equivalent `total_cost_usd` (tomahawkeer pays nothing extra; it is the
   yardstick for plan use). Every call record in decisions.jsonl carries `usage`; `Desk.usage_summary()` totals it per
   day for the page. Rows the engine writes (stop fills, close-out, new day, research grading) are NOT calls:
   `NOT_CLAUDE_CALLS`, shared with grade_read.py and pinned identical by a test. Non-JSON output falls back to text.
14. **The grade is code, not judgment (10-31): `grade_read.py` / `GRADE.bat`** implements pre-registration PART 2
   exactly (G1, G2 with 2,000 random-timing copies, floor, stop-applied copies, research bootstrap). Below 60 trades it
   prints PROGRESS ONLY and a test fails if any verdict word appears. `--selftest` proves the machinery on fake data.
   Rulings it made (entry window = 9:45 … min(15:30, 15:45 − hold); nearest-rank p95) are in its docstring.
15. **The trade lab (10-31): `lab.py` / `LAB.bat`** replays every closed trade on its saved 1-minute bars: MFE/MAE,
   stop width in typical 1-minute ranges, stop ×0.5…×2 what-ifs, first hour vs later, re-entry after a stop. Zero
   Claude calls. It feeds (a) the end-of-day review ("MEASURED FACTS", ≤2,500 chars) and (b) each decision prompt
   ("Earlier today: stopped out of …"; plus today's typical 1-minute range as a noise yardstick). It rebuilds
   `data/lab_report.json` once per close-out for the page. Every table says HINDSIGHT on n trades; picking a stop
   multiplier from it is curve-fitting. Prompt/playbook changes are part of the tested system per the pre-reg; money
   limits are not touched. A lab error is logged and never blocks a trading call (test).
16. **Lean Claude calls (10-31, config `claude_lean`).** As the desk started it (`claude -p --tools ""`), Claude Code loaded
   tomahawkeer's whole interactive setup into EVERY desk call. CORRECTED 10-31 evening (GitHub reply on claude-code#106413,
   docs "Configure tool search"): the 426k was caused by `--tools ""` itself — it also removes ToolSearch, so every
   MCP/connector tool definition loads UPFRONT instead of deferred; a plain `claude -p` is ~60k. Every probe variant
   carried `--tools ""`. Lesson: `--tools ""` is NOT "lighter" with connectors on; always pair it with
   `--strict-mcp-config` (or `ENABLE_CLAUDEAI_MCP_SERVERS=false` to keep other MCP servers). Original note: a one-line health check measured 429,388 input tokens (426k = connectors /
   MCP servers). TOKEN_PROBE.bat measured five variants on his PC; variant E (`--strict-mcp-config`,
   `--disable-slash-commands`, six `CLAUDE_CODE_DISABLE_*` env switches, the desk's own system prompt via
   `--system-prompt-file`) answered correctly with 600 tokens on the same SUBSCRIPTION login. Research keeps Claude
   Code's prompt (web tools). **Never use `--bare`: it ignores the subscription login and needs an API key.** A test
   pins config to the measured winner. Re-run TOKEN_PROBE.bat after any Claude Code update that looks odd.
17. **Phone push alerts (10-31, `notify.py`, config `alerts`).** ntfy.sh: free, no account, no key (rule 7 holds). Plain
   urllib POST to `<server>/<topic>` with Title/Priority/Tags headers (ASCII-only; body UTF-8). The topic is
   `daydesk-` + `secrets.token_urlsafe(12)`, made once in `data/alerts_topic.txt`; never in config.json, never in the
   log; the page shows it only so tomahawkeer can subscribe. Levels per event: loud=5, normal=3, silent=2, off=not sent.
   `Notifier.send()` never raises: queue + daemon sender, 3 tries with 2 s/4 s backoff, then logged; at most one
   message per 10 s (later ones wait), the same message within 60 s is dropped. Hooks (each wrapped in try; a test pins
   that a broken notifier still lets a buy happen): `Desk.ask` (buy, Claude sell), `Desk.step` (resting stop/target
   exits; breaker the first time each day, `breaker_alert_day`), `run_research` (silent summary or failure),
   `Desk.maybe_recap` (once a day, `recap_day`, at `recap_minute` 3:50, called after `end_of_day_review` in the same
   close-out pass, so the review's first sentence is in it; a late start reviews then recaps), `Health.watch_problem` on
   the HEARTBEAT thread (not the loop, which may be what is stuck): verdict not plain WORKING for
   `desk_problem_minutes` -> one loud alert per episode; a stale feed error after hours never rings. Page: Controls ->
   Phone alerts, `POST /api/alert-test` (sends now, normal priority). Tests: `tests/test_notify.py` (all fakes, no network).
18. **Fill honesty (10-31, pre-reg Amendment A3).** `Book.check_exits(sym, bars, now)` marks a bar checked only once it
   has FINISHED (t + 60 <= now) and re-checks the forming bar every poll (the old code missed stops touched late in a
   minute: 3 of the first 10 trades, net $5.32 kinder than honest). Scanner and Claude see finished bars only. Buys and
   Claude sells fill at the price re-fetched AFTER Claude answers (`Desk.fresh_price`; refused if older than
   `max_fill_staleness_s` or already through the stop); rows log ask_price, fill_price, drift_bps, latency_s.
19. **Start-up guards (10-31).** `engine.FROZEN_LIMITS` must equal config.json or new buys PAUSE with a red banner + heartbeat
   "ATTENTION" (changing one = pre-reg amendment + both places). `Book.books_problem()` reconciles cash with trades.csv;
   `Book._repair_double_sale()` closes a position already in trades.csv (crash between trade row and state save).
   `no_trade_days` (11-27, 12-24 early closes) = no buys, no research.
20. **Learning tools (10-31, all zero Claude calls, all HINDSIGHT/diagnostic, never the grade):** `decision_score.py`
   (every buy AND pass replayed with one neutral bracket: stop 2 typical ranges, target 1.5R), `trigger_lab.py` (the real
   scanner replayed on every archived day vs random minutes; `--confirm-after` writes a stamp and refuses a rerun;
   `BACKFILL_30D.bat` keeps a 30-day 1-min archive), rule credit (lessons carry stable R-ids from a one-way
   `last_rule_id`; decisions cite `rules`; "solid" needs SOLID_MIN_TRADES closed trades over SOLID_MIN_DAYS days, enforced
   in code), `p_win` in the decision JSON for calibration, version stamps `v` + full prompt/reply archive in
   `data/prompts/<day>.jsonl`. All three reports rebuild at the close-out (`refresh_lab`) and feed the review.
21. **Behaviour changes (10-31, scanner level, logged):** hard re-entry lock after a STOP (`reentry_lock_after_stop_min`
   60; rest of day after `max_stops_per_stock_per_day` 2; persisted in cooldowns.json; shown on the watchlist) and a
   one-time "protect-gain" question when a position is up `protect_gain_at_r` (1.0) risk-units (`stop0` = original stop).
22. **Research preview + search check (10-31 weekend).** Saturday's lean research said "market closed", made no
   searches and returned nothing, so a weekend run could never prove the web search works. `Desk.research_session(now)`:
   before 4 PM on a weekday the desk trades = today; otherwise a PREVIEW of the next weekday not in `no_trade_days`
   (saved under that date, `preview: true`, never auto-adds; the 8:45 run that morning replaces it). RESEARCH_SYSTEM
   says always search. A research row whose usage shows no search and ≤1 turn gets "WARNING: no web search was made".
   `pick_results.jsonl` rows carry `preview` (a failed 8:45 run would leave preview picks being graded).
23. **Dress rehearsal (10-31 weekend): `rehearsal.py` / `REHEARSAL.bat`.** Replays a saved day (`data/bars/<day>.json`)
   through the REAL Desk on a simulated clock (`day_desk.time`/`health.time` swapped for `SimTime`, restored after),
   with a scripted stand-in Claude (random but seeded; first fresh ask is always a 5%-stop buy that must be refused,
   the second always a good buy; ~2% garbage, ~2% timeouts), a lunchtime restart, an 11:00 simulated Yahoo outage,
   and ~30 PASS/FAIL checks (limits, flat by 3:45, books reconcile, re-entry lock, research/lab/review/recap/bars at the
   close-out, version stamps, usage, page data builds). Zero Claude calls, temp folder, nothing real touched.
   `tests/test_rehearsal.py` runs the latest saved day. A planted bug (close-out off) turned 12 checks red.
24. **Claude-can't-be-reached alarm (10-31 weekend).** Docs: `--bare` "will become the default for -p in a future
   release" and it ignores the subscription login. `Desk.note_claude_result()` (trading, review, research calls): a
   login-type error (`LOGIN_WORDS`) or `alerts.claude_fail_alert_after` (3) failures in a row add a "CLAUDE CAN'T BE
   REACHED" banner (page + heartbeat ATTENTION); a good call clears it. `Health.watch_problem` now treats ANY
   ATTENTION banner as a problem (it used to ignore them: verdict still started "WORKING:") and sends one loud
   "Day Desk needs attention" alert after `desk_problem_minutes`. Our GitHub issue: anthropics/claude-code#106413.
25. **Phone page (10-31 weekend).** On phones, long reference sections (test, usage, scorecard, lab, lessons, closed
   trades) fold to a one-line summary (tap the heading; remembered in localStorage; the jump bar opens its target);
   "why" text moves to a full-width line under its row (`tr.prow`) and is clamped to 3 lines (tap to read all; stays
   open across the 10 s refresh); minor columns hide (`.ph-hide`); closed trades show the latest 15 + "Show all".
   Measured at 390 px: 11,761 px tall → 2,760. Desktop layout unchanged. Click-tested in headless Chromium.

26. **Base rates in every fresh decision (10-31 weekend, config `base_rates_in_prompt`).** The 21-day trigger lab
   (10-02..10-30, 1,466 fires, 23 stocks) found no scanner trigger with mean R clearly above 0 (momentum +0.04,
   flush_bounce −0.02, vwap_reclaim −0.05, breakout −0.03; flush only "beats random" because random was −0.25): the
   scanner is an alarm clock, not an edge. The decision score had Claude's buys at −0.53 R vs passes −0.06 R. Those
   numbers used to reach Claude only in the evening review. `Desk.base_rates(kind, held)` now adds them (from
   `data/trigger_lab.json` + `data/decision_score.json`, cached on mtime, never blocking) to every NOT-held decision
   prompt, and asks a buy to say what makes this case better than the usual one. Prompt change = part of the tested
   system (pre-reg). Note: `usage.searches` is NOT reliable for Claude Code's WebSearch (reported 0 on a research run
   that searched 20 turns); use `turns`.
