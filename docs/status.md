# Project status

*As of 26 November 2026.* This is a snapshot. The README is the reference, and each
figure below can be reproduced with the command shown next to it.

## What exists

| Part | State | Command |
|---|---|---|
| Public-data pipeline: load, enrich, score, funding, news, case files, dashboard | Working. Incremental. Records each run and writes a digest | `uv run python -m pipeline` |
| Scoring engine: 8 signals plus hedge and exit deductions | Working. Validated as below | `uv run python -m scoring.run` |
| AI case files | Working. Number, language and rubric checks; 10 of 10 passed on the latest runs | `uv run python -m cases.run` |
| Live watch on open markets | Working. First run: 150 markets, 287,939 trades, 6 alerts | `uv run python -m watch.run_live` |
| Analyst review loop | Working. Dashboard decisions, audit trail, per-signal statistics | `uv run python -m app.review` |
| Operator data (sportsbook CSV) | Working. Example in `examples/operator/` | `uv run python -m ingest.csv_loader` |
| Event dossiers | One page per flagged story: price path with flagged buys, lead times, accounts | `./grib dossiers` |
| Live-watch scorecard | Earlier live alerts followed up once their markets resolve: wins vs the odds paid | `./grib scorecard` |
| Market screen (no accounts) | Ranks markets from anonymous trades; Kalshi loader included | `./grib screen` |
| Category index | Categories compared on behaviour alone (checks the category prior) | `./grib categories` |
| Quick demo | Maduro and Nobel in a small universe, ~15 min | `uv run python -m demo` |
| Notifications | New alerts posted to a Slack-compatible webhook (`GRIB_ALERT_WEBHOOK_URL`) | `watch.run_live --every 3` |
| Unit cost | ~7.6¢ per AI case file (33 written) | printed by `cases.run` |
| Tests | 134 passing | `./grib test` |

**Clean-machine run (26 November).** A fresh clone, `uv sync`, then the full pipeline on 200 new
markets: load 8 min, account enrichment 14 min, scoring 7 s, on-chain funding 11 min, news 5 min
(GDELT refused, Google News answered all 14 markets), 10 AI case files 7 min (≈$0.83, ≈8¢ each),
dossiers, screen pages and dashboard under 1 s. 9 of 10 case files passed every automated check;
the tenth was held for attention because the draft used a banned word ("insider").

## What it shows

**CFTC complaint match.** The engine's #1 alert is 436,760 "Yes" shares of "Maduro out by
January 31, 2026?", bought 31 Dec 2025–3 Jan 2026. The CFTC's complaint (SDNY 26-cv-3369)
alleges "more than 436,000 Yes shares" of that market in that period.

**Prospective flag.** On 24 November, before any reporting was seen, the first run put a
Jensen Huang/NVIDIA position at #1. Reports the same days described suspicious clusters on
those markets.

**Publicly reported cases.** The engine was run unchanged and saw trades only. Ranks are
per story, out of 128 events (`uv run python -m eval.known_cases`):

- US strikes on Iran, February 2026 (out of sample): #1
- Maduro (January 2026): #2
- Iran ceasefire, April 2026 (out of sample): #4
- Nobel Peace Prize 2025: #5 (tied with one ordinary market at the same score)
- Google Year in Search 2025: #7

*Out of sample* means the case was added after every engine change. In a raw position
ranking (10,908 positions), the February strikes flood the list with 167 alerts, which is
why the review queue groups alerts by event.

**Without account ids.** The market screen sees only each trade's time, side, price and size. On the
same universe it ranks the reported stories #1, #2, #3, #14 and #19 of 126 (`./grib screen-cases`). This is what
a venue with an anonymised feed, such as Kalshi's public trades, could run.

**Live watch, replayed using only the data available at each moment**
(`uv run python -m watch.live --replay`):

- Maduro: flagged 16.6 hours before the news, on the same #1 account.
- Nobel: flagged 10 hours before the announcement, on the account that started the price
  run-up.
- Iran ceasefire and US strikes on Iran (both out of sample): first alerts 6–14 days before.
- Ordinary markets: 6.1 alerts per 1,000 market-days.

**Planted trades** (`uv run python -m eval.backtest --markets 200 --seed 1`):

- 82–84% of informed trades rank in the top 1%.
- Accounts that sold before the news reach the review threshold 0–3% of the time.

## What changed because of the evidence

The first run on real cases missed most of the Nobel case. That run exposed four engine
flaws, and each was fixed and re-checked against the backtest:

- timing baseline contaminated by the leak itself
- no credit for starting a price run-up
- exits into the run-up penalised
- conviction measured against volume that came after the news

A code review then found ten more issues, all fixed, each with a regression test. Most
concerned operator data, and one concerned news searches outside GDELT's three-month
window. Details are in the README and the commit history.

## Known limits

- **Five public cases** (two of them out of sample) is still a small sample.
- **Sports and 5-minute crypto markets** are blind spots by design.
- **Google-style cases** (many modest wins across sibling markets) are harder to catch
  than single large bets.
- **Older wallet funding** needs a paid archive RPC node: 37 of 82 wallets in the demo
  could not be scanned.
- **News** comes from GDELT, with Google News as the fallback when GDELT is unreachable or the
  window is older than GDELT's ~3 months. Google's dates are often day-precise and can be a day early,
  so such items are timed at the end of the following day and never count as preceding a buy.
  Failed searches are marked as failed and retried, never reported as "no news".
- **The live watch** needs scheduling (every few hours) to protect payouts in practice. Its
  prospective scorecard has 6 alerts and none resolved yet; the replayed equivalent (5 of 17 won
  vs 3.6 implied) is in the right direction but not significant.
- **Free news sources are best effort.** On 26 November GDELT refused most requests with HTTP 429
  for minutes at a time (the Google News fallback now covers that). A production version should
  add a paid, minute-precise wire source.
- **Kalshi's public feed** appears to keep only about two months of trades and has no accounts, so
  only the market screen applies, and only to recently settled markets.

## Needs a decision

1. **Archive node key.** Choose a provider and add the key as `POLYGON_RPC_URL`, then run
   `uv run python -m ingest.funding --rescan`.
2. **Review threshold.** 50 works as a "definitely look" line. Ranking by the top 1% is the
   better cut if reviewers can handle about 70 alerts.
3. **Business model, fundraising amount, team.** These are open in `docs/investor-brief.md`.
4. **Publishing.** Nothing has been published. Outputs name no one and truncate wallets, but
   any public use of the known-case material needs care, because the reports behind those
   cases are allegations.
