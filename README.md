# Day Desk

An experiment: can an AI (Claude) make money day trading **after costs**, and can it prove it isn't just luck?

Day Desk is a **paper** day-trader that runs on a Windows PC. Cheap rules watch 1-minute bars; when a setup
appears they wake Claude (through Claude Code on a normal Claude subscription, no API key), which answers buy
or pass with a stop, a target and a reason. A separate engine enforces every money limit no matter what Claude
says. After each close, Claude reviews its own trades and passes against what happened next and rewrites its
playbook.

**Status: unproven.** No broker is connected and no real money moves. Whether it works is decided by a
pre-registered test (see [GRADING.md](GRADING.md)): it must be profitable after costs **and** beat 2,000
random-timing copies of its own trades. Until then, nothing here is evidence that it works. This is not
financial advice.

Things that may be useful to others even if the trading doesn't pan out:
- `grade_read.py`: the random-timing null (same stock, same day, same size, same holding time, random entry).
- `brain.py` / `token_probe.py`: running `claude -p` on a subscription at about 600 input tokens per call instead
  of 429k (the claude.ai connectors were being loaded into every call).
- `engine.py`: frozen money limits, resting stops walked over 1-minute bars, a books check that pauses buying if
  cash and positions stop adding up.
- `lab.py`, `decision_score.py`, `trigger_lab.py`: hindsight diagnostics for trades, passes and triggers.

The user guide below was written for the person running it.

---

Live **paper** day trading on your PC, with Claude making the calls. No broker is
connected and no real money moves.

## What it does

- **Every 30 seconds** it pulls 1-minute price bars (free, from Yahoo Finance) for the
  stocks on your watchlist, plus the Nasdaq (QQQ).
- **Stops and targets work like real resting orders.** They're checked against every
  1-minute bar, so a stop hit at 1:07 fills at 1:07's price.
- **A scanner decides when to wake Claude**, when one of your stocks:
  - breaks to a new high of the day on heavy volume,
  - climbs back above VWAP after sitting under it,
  - bounces off a hard flush, or
  - makes a strong 15-minute push that beats the Nasdaq.

  Claude gets the day's 5-minute chart, the last 30 one-minute bars, the Nasdaq's move
  and fresh headlines. It answers buy or pass, with the size, a stop, a target and the
  reason. Claude is asked through **Claude Code on this PC, on your normal Claude
  subscription**, so there's **no API key and no separate bill**. Calls count against your
  plan's usage limits, like chatting does.
- **Open positions** are re-checked by Claude every 30 minutes, so it can raise the stop
  or cut a loser.
- **Every trading morning at 8:45 AM ET it researches the news.** Claude Code searches the
  web for pre-market movers with real catalysts (earnings, upgrades, FDA, deals) and today's
  scheduled events. It picks 3 to 6 stocks worth watching, each with the catalyst, what would
  make it a buy, what would rule it out, key levels, and a **conviction** rating.
  - **High conviction** (prime for a profitable day) is **added to the watchlist
    automatically for that day**. That's tomahawkeer's permission, 10-28. The researcher is told to be
    stingy: usually 0 to 2 a day.
  - Auto-added stocks come off the next morning unless still held or picked high again.
    Stocks **you** added are never removed.
  - Medium and low picks show on your phone under **Morning research** with an
    **Add to watchlist** button.
  - `auto_add_picks` in `config.json`: `"high"` (the default), `false` (never auto-add) or
    `"all"`.
  - **Research now** on the phone runs it again any time.
  - The research is fed into Claude's trade decisions on those stocks.
  - The end-of-day review grades how the picks actually did.
  - Every pick gets live prices all day, even ones NOT on your watchlist. Those are
    **watch-only**: priced so the research can be graded, never traded. The phone page shows
    each pick's move since the open. At 3:45 PM each pick's 9:45-to-3:45 move against the
    Nasdaq (QQQ) is saved to `data/pick_results.jsonl`.
  - It runs once a morning, on Opus, with web search only. It can't touch any files.
- **It learns from its own trading.**
  - Every buy is tagged with the **strategy** behind it (e.g. `vwap-reclaim`), and a
    **scorecard** keeps each strategy's trades, win rate and P&L.
  - After the 3:45 PM close-out, Claude reviews its day once: every trade, and every pass
    next to what the stock did in the following hour. It grades each strategy (keep /
    adjust / drop / needs more data) and rewrites a **playbook** of up to 15 rules, each
    marked tentative or solid.
  - Every later decision reads the scorecard and the playbook. It costs one extra Claude
    call a day.
- **Rules enforced in code, whatever Claude says:**
  - $10,000 start, at most 4 positions of at most $2,500 each
  - long only, and only stocks on your watchlist
  - stop no more than 3% away
  - no new buys after 3:30 PM, everything sold at 3:45 PM
  - 0.01% cost per side (Robinhood spread)
  - at most 100 Claude calls a day (`max_calls_per_day`)
- **A phone page** at port 8790 shows the scoreboard, open positions, every call Claude
  made and why, and the closed trades. It also has your watchlist editor, **Pause** and
  **Sell all**.

## Set it up (once)

1. Double-click **SETUP.bat**. It builds the program's own Python environment, which
   takes a few minutes. At the end it tells you whether Claude Code is installed.
2. If it says Claude Code is NOT installed, double-click **INSTALL_CLAUDE_CODE.bat**. It
   runs Anthropic's official installer, then opens Claude Code once so you can sign in.
   **Sign in with your Claude account (your subscription), not an API key.** Type `/exit`
   when it's done.
   - Without Claude Code it still runs in **watch-only** mode: it shows what it *would*
     ask Claude and never trades.
3. Right-click **ALLOW_TAILSCALE_FIREWALL.bat** and choose **Run as administrator**. This
   lets only devices on your Tailscale network reach the page.

## Run it

- Double-click **RUN.bat** and leave the window open during market hours. Closing the
  window stops it.
- No password: the page just opens. The Tailscale-only firewall rule is what keeps it
  private. (To turn a key back on, set `"require_access_key": true` in `config.json`.)
- **On your phone** (Tailscale on): open `http://YOUR-PC-NAME:8790`, or use your PC's
  Tailscale address, which looks like `http://100.x.y.z:8790`.
- **On this PC:** open `http://localhost:8790`.

If Windows asks whether to allow Python through the firewall, say **Allow**. The
Tailscale rule from step 3 is what lets your phone in.

## Files

| file | what it is |
|---|---|
| `config.json` | every setting (money limits, times, scanner sensitivity, model, daily call cap), each explained |
| `data/trades.csv` | every closed trade with the reason in and out |
| `data/decisions.jsonl` | every time Claude was asked: why it was woken, what it said, what happened |
| `data/lessons.json` | the current playbook, strategy verdicts and the last day's summary |
| `data/lessons_history.jsonl` | every version of the playbook, one per day, so you can see how it changed |
| `data/picks.json` | this morning's research: market backdrop and picks |
| `data/bars/<day>.json` | every 1-minute bar the desk saw that day, saved at 3:45 (needed to grade the test) |
| `data/pick_results.jsonl` | one line per pick per day: its 9:45-3:45 move vs QQQ (grades the research) |
| `data/auto_added.json` | which watchlist stocks the research added, and on which day |
| `data/picks_history.jsonl` | every morning's research, so picks can be checked later |
| `data/state.json` | cash and open positions |
| `notify.py` + `data/alerts_topic.txt` | phone push alerts through the free **ntfy** app (no account, no bill). Install ntfy from the app store, tap **+**, subscribe to the topic the page shows under **Controls → Phone alerts**, then tap **Send test alert**. Stops, targets, the daily loss limit and desk problems ring loud; a recap comes at 3:50 PM (normal); buys, sells and research arrive silently. If a Claude call suddenly reads far more input tokens than its kind normally does (the token ceiling, `token_ceiling` in `config.json`), the page shows an amber banner and the first one each day rings loud. Levels per event (loud / normal / silent / off) are under `alerts` in `config.json`. Anyone with the topic can read the alerts, so keep it to yourself |
| `data/day_desk.log` | the running log |
| `TEST.bat` | runs the 219 checks that pin the rules above |
| `GRADE.bat` | the pre-registered grade. Under 60 closed trades it only shows progress (no verdict, on purpose); at 60 it prints Read 1. Also writes `data/grade_report.txt` |
| `TRIGGER_LAB.bat` | replays the scanner's wake-up rules on every saved day vs random minutes: which triggers earn a Claude call (no Claude calls). `TRIGGER_LAB.bat --confirm-after 2026-11-06` checks a change on later days, once |
| `DECISION_SCORE.bat` | scores every buy AND every pass Claude made with one standard trade: is its judgment adding anything? (hindsight, not the grade) |
| `BACKFILL_30D.bat` | fills the 1-minute price archive back 30 days from Yahoo (never overwrites, never today) |
| `TOKEN_PROBE.bat` | measures how many tokens one Claude call drags in (run after a Claude Code update) |
| `TOKEN_PROBE_ONE_SERVER.bat` | the same health check with exactly ONE small MCP server kept (and the connectors-off switch), to see what a single server costs |
| `REHEARSAL.bat` | dress rehearsal: replays a saved trading day through the whole desk with a stand-in for Claude (zero Claude calls), then prints PASS/FAIL checks. Run after any change, before a trading day |
| `LAB.bat` | the trade lab: replays every closed trade with tighter/wider stops on its real 1-minute bars (hindsight, no Claude calls). Writes `data/lab_report.txt`; the page shows it too |

## Costs

**No extra charge.** Claude is reached through Claude Code on your existing subscription.
The program removes any `ANTHROPIC_API_KEY` from the environment before every call,
because that variable is the one thing that would make Claude Code bill an API account
instead. The cost is your plan's usage: the 100-calls-a-day cap in `config.json` keeps it
modest, and the model is `opus` (tomahawkeer 10-28), the strongest; switch to `sonnet` in `config.json` if usage runs low. Price data from
Yahoo is free.

## If something breaks

- **The page says "Can't reach Day Desk":** RUN.bat isn't running, or the PC is asleep.
- **Claude's calls show "Claude call failed: not logged in":** run INSTALL_CLAUDE_CODE.bat
  again and sign in.
- **The red pill says "price feed failed":** Yahoo had a hiccup. Day Desk retries every
  30 seconds on its own.
- **Morning research shows "failed":** usually a Claude Code hiccup. Tap **Research now**.
- **It was off at the close with positions open:** at the next start of day it sells them
  at the latest price and logs why. Nothing is ever carried overnight on purpose.

## Is it actually running? (added 10-29)

- **Easiest:** open `http://<this PC's Tailscale name>:8790/api/ping` on your phone. If it loads, the desk is alive,
  and the first line (`verdict`) says whether the trading loop is OK too.
- **The file:** `data\heartbeat.json` is rewritten every 30 seconds. If it is more than a few minutes old, Day Desk is
  not running: start RUN.bat.
- **The mailbox (mostly for Claude to use while you're at work):** put a text file with a brand-new name in
  `data\inbox\` containing one word, and the answer shows up in `data\outbox\` with the same name, in about 10 seconds:
  `ping` (alive?), `feed` (can it get prices from Yahoo right now?), `claude` (can it reach Claude on your
  subscription? at most 5 a day), `pause` / `resume` (a kill switch: same as the page's Pause button, so no new buys
  while open positions keep their stops and still get sold by 3:45), `help`. Never reuse a file name; a used name is
  ignored on purpose.

## Control arm (pre-registration Addendum A4, from Mon 11-02)
Does Claude's judgment beat a two-line rule? Every time Claude is woken about a stock it doesn't hold, three "traders"
pick from the same list: take everything (ALL), take only what a fixed rule allows (GATED: price above VWAP and
stronger than the Nasdaq since the open), or take what Claude bought (CLAUDE). All three are scored with the same
standard trade. It shows on the page's Trade lab panel, in `DECISION_SCORE.bat` and in `GRADE.bat`. Reported only: it
never changes the pass/fail grade, and it only gets a reading at Read 1 (60 trades) and Read 2 (120).
