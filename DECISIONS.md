# DECISIONS

Where the brief was ambiguous or the world disagreed with it, this file records what was chosen and why. All entries dated 2026-11-10 unless noted.

## Blocking facts

1. **Seed files were not provided.** `calls_scored.csv`, `forward_ledger.csv` and `claude/jonah-full-channel-verdict-2026-11-10.md` do not exist on the build machine (searched Downloads, Desktop, Documents and the recent project folders). So the two seed acceptance checks (98% entry match / +10.1% on n=151, and Hit/Missed/Open = 5/3/11) **have not been run**. `jonah-ledger seed-check` and two pytest cases are in place; they skip loudly ("NOT VERIFIED") until the files are dropped into `data/seed/`. The column mapping is guessed from name patterns because the headers were never seen; every guess is printed and can be overridden with `--map key=column`.
2. **No `ANTHROPIC_API_KEY` in the build environment.** The extraction dry run therefore ran the transcript stage only (5 recent videos, captions fetched OK: 591 to 868 words each). LLM extraction is covered by unit tests with a fake client, not by a live call. Run `jonah-ledger dry-run -n 5` with a key to eyeball real output.

## Discovery and transcripts

3. **RSS returns 404 from the build machine** (`feeds/videos.xml?channel_id=UCKZDPyNulHpz20PpGSMbW5g`, with and without a browser user agent, repeated). Discovery tries RSS first and falls back to `yt-dlp --flat-playlist` on the channel's `/videos` and `/shorts` tabs. The fallback has no timestamps, so publish time comes from yt-dlp's per-video metadata (`timestamp`, UTC epoch) fetched during the transcript step. Whether RSS works from GitHub runners is unknown.
4. Channel id for `@carsandinvesting` is `UCKZDPyNulHpz20PpGSMbW5g`, parsed from the channel page and cached in `data/channel.json`.
5. **First run is forward-only.** With an empty `seen_videos.json` and an empty ledger, only videos newer than 3 days are processed; older ones are recorded as `baseline_skipped`. The backtest already covers history.
6. **Unreadable is not "no calls".** A video without captions, blocked, or live/upcoming is `unreadable` (live/upcoming are simply retried). Unreadable videos are retried on later polls for up to 8 attempts or 72 hours, because auto-captions often appear hours after upload. Videos that were read and had no calls are `no_calls`. The weekly report counts videos still unreadable. Three or more `blocked` results in one run posts a one-per-day Discord warning pointing at `scripts/local_run.sh`.
7. Captions: English auto-subs in `json3`; `en` preferred over `en-orig`. Transcripts live only in a temp dir that is deleted before the function returns.

## Extraction and ledger

8. **Labels.** `buying`, `wants`, `short`, `mention`. `direction` is `long`/`short`: buying and wants are always long, short is always short, a mention takes the direction of what he says. "Long calls" everywhere (gate, headline stats) means `buying` + `wants`. When one ticker has several labels in a video, the strongest is kept: buying > wants > short > mention.
9. **Quote handling.** Quotes over 15 words are cut to the first 15 (counted). A quote must also be grounded: at least 60% of its words must occur in the transcript, otherwise the call is dropped and counted (`dropped_ungrounded_quote`). This guards against hallucinated calls; the cost is that a heavily paraphrased real call is lost. Counts are stored per video in `seen_videos.json`.
10. A `deadline` is kept only with a `target` and only if it is not before the video date.
11. **Extra ledger fields** beyond the brief's list: `prompt_hash` and `model` (the brief asks for the prompt hash to be stored), `late_extraction`, and `supersedes` on corrections. `extractor_version` is `1.0.0+<first 12 chars of the prompt hash>`. Retractions are a correction row with `strength: "retracted"` and `supersedes`.
12. **Late extraction.** A call is flagged `late_extraction: true` if it was logged after 09:30 ET of the first weekday after the video's ET date (holidays ignored, which can only under-flag). Scoring still uses the rule-based entry, but the scorecard counts late rows so a slow poll cannot quietly inflate results.
13. **Ledger tamper limits.** `ledger verify` fails on any changed byte, reordered, deleted or inserted row, because each line must equal its canonical serialisation and each hash covers the previous one. It cannot detect removal of the **last** rows (a shorter chain is still valid). The head hash is printed by `ledger head` and posted in the weekly digest, and git history is the second witness.
14. `temperature=0` is sent; if the API rejects it for the chosen model, the call is retried once without it. `JONAH_MODEL` empty or unset means `claude-sonnet-5-5`.

## Prices and scoring

15. **Entry** is the open of the first session strictly after the video's US/Eastern calendar date (so a Monday-morning upload enters Tuesday). Sessions come from the ticker's own price index, so holidays work without a calendar.
16. **Horizons.** The entry session counts as session 1, so "1st session close" is the entry-day close and the 60th is 59 sessions later. The brief does not say which. This is one switch (`ENTRY_DAY_IS_SESSION_1` in `config.py`) and `seed-check` reports the 60-session mean under both so the real convention can be confirmed against the seed. It has not been confirmed.
17. **Cost** is 10 bps per call (30 bps if the raw entry open is under $5), deducted once. "Per trade" could also mean per leg (20 bps round trip). One call = one trade was chosen. SPY gets no cost. For a short call, net = -gross - cost and excess = net - (-SPY return).
18. **Adjusted prices.** yfinance is pulled with `auto_adjust=False`; adjusted open = open x (Adj Close / Close). Returns use adjusted entry open and adjusted exit close; the under-$5 test and price-target comparisons use raw values.
19. **Hit rate** in the scorecard is the share of resolved long calls with excess return above 0. A call with no price data is `no_price_data`; one whose data ends more than 10 sessions before SPY's is `stale_data` (delisting survivorship is therefore visible, not hidden).
20. **Price targets** are checked against raw daily highs (lows for shorts) from the entry session through the deadline. Hit = reached inside the window; Missed = deadline passed without a hit; Open = otherwise (including no deadline).
21. Descriptive fields (depth below the 252-session high using prior sessions only, trailing 60-day return, 60-day beta) are logged and never used to filter.
22. **Gate** uses calendar month of the video's ET date for month-equal weighting and requires strictly positive values. It is a message only.

## Alerts and operations

23. New-call alerts go out for all four labels (the label is shown), grouped into as few Discord messages as limits allow. Mentions are noisy but are part of the record; silence them with a code change if wanted.
24. Alerts are deduplicated through `data/alerts_state.json`, so a re-run never re-posts a call or a target event. Embeds are clipped to Discord limits (title 256, description 4096, field value 1024, 25 fields, 6000 per embed set, 10 embeds per message). `allowed_mentions` is empty so nothing can ping.
25. The stale-bot heartbeat runs inside `score.yml` (not `poll.yml`), so it still fires if the poll workflow is the thing that died. It warns once per 24 hours.
26. GitHub's cron is best effort and can run late. The three workflows share one concurrency group so they never push over each other. Scheduled workflows are paused after 60 days of repository inactivity on public repos; the repo is meant to be private.

## IPO watcher (phase 2)

27. **Source: the Nasdaq IPO calendar JSON first** (reachable from the build machine, includes proposed price range and pricing dates), **EDGAR 424B4 full-text search as fallback**. If neither is reachable the run logs a skip and exits 0. Neither was tested from GitHub runners; Nasdaq's API is known to block some cloud ranges.
28. EDGAR asks clients to identify themselves in the user agent. The default is a generic `jonah-ledger research bot`; set `SEC_USER_AGENT` (an Actions variable) to something with a contact if desired. No personal details are sent by default.
29. Outcomes: for a priced deal with a single numeric offer price, the first trading bar within 4 days of the pricing date gives day-one open/close (raw). Ranged or missing prices are not measured (no clean offer price). The assumption that Nasdaq's `pricedDate` is on or just before first trade day is untested. Deals are listed in calendar order, never ranked or filtered.

## Cost estimate (unverified)

30. The README's cost figure assumes Sonnet-class pricing of $3 / $15 per million input / output tokens because the price of `claude-sonnet-5-5` was not available to the build. Check current pricing.
