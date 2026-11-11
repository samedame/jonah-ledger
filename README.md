# jonah-ledger

A forward-measurement instrument for the YouTube channel **@carsandinvesting**. It watches for new videos, extracts dated stock calls, writes them to a tamper-evident ledger, scores them against SPY as the horizons complete, and posts Discord alerts.

**PAPER ONLY, not a signal.** Research tool, not financial advice. It places no orders, imports no broker library and never asks for broker credentials. A passing result means "passed these checks on this data", nothing more.

## Why it is built this way

The full-channel backtest ran 10 primary tests and only two passed. One needs an IPO allocation (cannot be automated). The other is his dated "I'm buying X" calls: +10.1% mean 60-session excess vs SPY (n=151), but only +2.5% without his 10 best calls, +2.4% with calendar months weighted equally, mean beta 1.5, and most calls came after the March 2026 low. That is fragile, so this repo does not trade it. It collects out-of-sample calls and tells you when (if ever) the same checks pass going forward. None of the trade setups he teaches is implemented.

## Pipeline

| Step | Module | What it does |
|---|---|---|
| discover | `discover.py` | Channel RSS, falling back to `yt-dlp` listing of /videos and /shorts. Skips ids in `data/seen_videos.json`. |
| transcript | `transcript.py` | `yt-dlp --skip-download --write-auto-subs` into a temp dir, deleted right away. No captions or blocked = `unreadable` (counted, never treated as "no calls"). |
| extract | `extract.py` | Claude (`JONAH_MODEL`, temperature 0, forced tool call = strict JSON). Code re-validates everything: tickers must exist in the Nasdaq Trader list, quotes <= 15 words and grounded in the transcript, one row per ticker per video. |
| ledger | `ledger.py` | `data/calls.forward.jsonl`, append-only, hash-chained. Corrections are new rows with `supersedes`. |
| prices | `prices.py` | yfinance daily bars, cached. Entry = first session open strictly after the video's US/Eastern date; marks at sessions 1/5/20/60/120/250; 10 bps cost (30 bps under $5); excess vs SPY. |
| score | `score.py` | Daily after the close, plus the Saturday markdown scorecard in `reports/weekly/`. |
| gate | `gate.py` | `COLLECTING` until 30 long calls have finished 60 sessions; then `PASS`/`FAIL`. Message only. |
| alerts | `alerts.py` | Discord embeds: new call, weekly digest, target hit / deadline missed, stale-bot warning. Quiet on days with nothing new. |
| ipo_watch | `ipo_watch.py` | Alert-only IPO calendar watcher; logs offer-to-day-one outcomes. |

Judgement calls (conventions, fallbacks, what could not be verified) are in [DECISIONS.md](DECISIONS.md).

## Setup

1. Create the **private** repo `samedame/jonah-ledger` and push this folder to it.
2. Settings > Secrets and variables > Actions > **Secrets**: `ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`.
3. Settings > Actions > General > Workflow permissions > **Read and write permissions**.
4. Actions tab > **poll** > Run workflow (`workflow_dispatch`). Check the log and the Discord channel.
5. Optional repository **variables**: `JONAH_MODEL`, `JONAH_ALERTS` (set `off` to silence Discord but keep logging), `SEC_USER_AGENT`.

Local development:

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/jonah-ledger ledger verify
```

## Commands

```text
jonah-ledger poll [--no-alerts] [--max-videos N]   discover -> transcript -> extract -> ledger -> alerts
jonah-ledger score [--no-alerts]                   resolve horizons, write data/outcomes.csv, target alerts
jonah-ledger weekly [--no-alerts]                  scorecard in reports/weekly/ + Discord digest
jonah-ledger heartbeat                             stale-bot warning if no discovery success in 48h
jonah-ledger ledger verify | head | show -n 10     recompute the hash chain (exit 1 on any problem)
jonah-ledger ipo-watch [--no-alerts]               IPO calendar alerts and day-one outcome log
jonah-ledger dry-run -n 5 [--no-llm]               print what would be extracted from the latest N videos; writes nothing
jonah-ledger seed-check [--asof YYYY-MM-DD] [--map key=col]   acceptance checks against data/seed/*.csv
```

## Operations

**GitHub Actions** (default). `poll.yml` every 3 hours, `score.yml` weekdays 22:30 UTC (also runs the stale-bot heartbeat and the IPO watcher), `weekly.yml` Saturday. Each verifies the ledger, runs, verifies again, and commits `data/` and `reports/` back with `[skip ci]`. All three share one concurrency group.

**Local fallback** if YouTube blocks GitHub's IP ranges (symptom: videos pile up as `unreadable` with reason `blocked`, and you get the "transcript fetches look blocked" warning). Same code, run from your own computer:

```bash
cp .env.example .env          # fill in ANTHROPIC_API_KEY and DISCORD_WEBHOOK_URL
scripts/local_run.sh          # pull, verify, poll, verify, commit, push
```

Schedule it with cron (`0 */3 * * * /path/to/jonah-ledger/scripts/local_run.sh >> /tmp/jonah.log 2>&1`) or launchd (`scripts/com.jonah-ledger.poll.plist.example`). If you use the local path, disable the `poll` workflow so the two do not both write.

**Kill switch.** `JONAH_ALERTS=off` silences Discord and keeps logging everything else.

## Cost estimate

One Claude call per video. A short video transcript is ~600 to 900 words (about 1 to 1.5k tokens); a 20 to 30 minute one is ~5 to 8k words (about 7 to 11k tokens). Add ~1k tokens of fixed prompt and ~0.3 to 0.6k tokens of output. At an **assumed** $3 / $15 per million input / output tokens (Sonnet-class; the price of `claude-sonnet-5-5` was not checked) that is roughly 1 cent for a short video and 3 to 6 cents for a long one, so well under $2 a month at a few videos a week. GitHub Actions minutes for a private repo are a few minutes per run. yfinance, Nasdaq Trader and the Nasdaq/EDGAR calendars are free.

## The gate

`COLLECTING` until at least 30 resolved long calls (`buying` or `wants`) have finished 60 sessions. Then `PASS` only if all three hold: mean excess vs SPY > 0, still > 0 after dropping the best two calls, and the month-equal-weighted mean > 0. Otherwise `FAIL`. On `PASS` the only sizing it mentions is 1% of equity per call, cash not margin, and a cap on total exposure (his calls cluster; an earlier simulation at 2% per call ended up fully invested). It never trades.

## Data files

| File | Purpose |
|---|---|
| `data/calls.forward.jsonl` | The ledger (append-only, hash-chained). Fields: `call_id, video_id, published_utc, ticker, direction, strength, target, deadline, quote, extractor_version, extracted_at, prev_hash, row_hash` plus `prompt_hash, model, late_extraction, supersedes`. |
| `data/seen_videos.json` | Per-video status: `extracted`, `no_calls`, `unreadable` (with reason and attempts), `baseline_skipped`. |
| `data/outcomes.csv` | Derived scoring table, rewritten daily. |
| `data/heartbeat.json`, `data/alerts_state.json` | Last discovery success; alert de-duplication. |
| `data/ipo_state.json`, `data/ipo_outcomes.jsonl` | IPO watcher state and the offer-to-day-one log. |
| `data/seed/` | `calls_scored.csv`, `forward_ledger.csv` from the backtest delivery (not included yet). |
| `data/prices_cache/` | Gitignored. |

Transcripts and caption text are never written to the repo.

## Known limits

- Auto-captions garble tickers; the extractor drops what it cannot resolve and counts it.
- yfinance data can be revised or missing for delisted names. `stale_data` and `no_price_data` calls are flagged in `outcomes.csv`.
- Truncating the last rows of the ledger is not detectable by the chain alone; compare the head hash in the weekly digest with git history.
- Beta, depth below the high and trailing return are logged for later study, never used to filter.
