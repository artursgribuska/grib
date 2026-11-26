# Five-minute demo

A walkthrough for a meeting. Everything shown is produced by the commands in the README.
Nothing is staged. Run `./grib pipeline --skip-ingest` (or `uv run python -m pipeline --skip-ingest`) beforehand so the outputs
are fresh.

## 1. The problem (30 s)

Prediction markets now trade around $120B a month. Regulators expect venues to watch for
insider trading themselves, but every account is pseudonymous. The only evidence is the
trading.

## 2. A real case, found blind (90 s)

Open `data/known_cases/dashboard/index.html` and look at the Nobel Peace Prize 2025 alert.

- The bar breaks the score into its signals. Point at timing, a fresh account, size, and
  price leadership.
- Expand the alert to show the reasons: the account bought $43k of the winner starting at
  22:42 UTC, ten hours before the announcement, and did most of the buying that took the
  price from 3¢ to 75¢. The case file adds that the account was first seen at 21:26 UTC,
  about an hour before its first buy.
- Make the key point: the engine was told nothing about this case. It ranked this position
  among the top 5 of 128 stories, alongside ordinary markets.
- Then the Maduro alert, which ranked #1. The case writer moved it from the rule-based
  Medium up to High and named its facts: 5 other flagged wallets on the event, and 8
  low-history buyers of the same long shot within 24 h.

## 2a. Show, don't tell (60 s)

Open `data/known_cases/timemachine.html`, pick Maduro and press play. The 5-day-old account starts
buying at 7¢ three days out; the live-watch alert fires 16.6 hours before the news; at the public
moment the engine's conclusion appears, with the CFTC complaint match.

## 2b. It would have warned them in time (60 s)

Run `uv run python -m watch.live --replay`. It replays history using only the data
available at each moment:

- Maduro: flagged **16.6 hours before the news**, on the same account, 5 days old and
  drip-buying at 7¢.
- Nobel: flagged **10 hours before** the announcement, on the account that started the
  run-up.

Then show `data/live/watch.md`, which is today's live alerts on open markets.

## 3. The case file (90 s)

Open the matching case file in `data/known_cases/cases/`. Show:

- the summary and timeline
- the innocent explanations it considered
- the rating rationale, and how that rationale names the rule it started from
- the footer: "every number traced to the evidence bundle", "language check passed"

The point: this is the analyst's first draft, ready for review, and that is where a
compliance team's time goes.

## 3b. The story at a glance (30 s)

From the dashboard's *Events with several flagged wallets* table, click an event to open its
**dossier** (`data/dossiers/`, or `data/known_cases/dossiers/` for the known cases): the winning
outcome's price path, every flagged first buy as a dot against the estimated public moment (hover
for details), and how much flagged money came in 6, 24 or 72 hours ahead.

## 4. The workflow (60 s)

Back on the main dashboard (`data/dashboard/index.html`):

- Pick a decision, add a note, then export.
- Run `uv run python -m app.review stats` to show precision per signal, built from
  reviewer decisions.
- Open `data/digest/latest.md`, the daily digest a team receives.

## 5. Their data, not just ours (30 s)

Run `uv run python -m ingest.csv_loader ...` on `examples/operator/`, which is a
sportsbook export in odds and stakes. The two planted accounts come out #1 and #2.

## 5b. No accounts at all (30 s)

`./grib screen-cases`: the market screen sees only times, sides, prices and sizes, as a venue
with an anonymised feed would. It still ranks the reported stories #1, #2, #3, #14 and #19 of 126.
`./grib kalshi` then `GRIB_DB_PATH=data/kalshi.duckdb ./grib screen --rebuild` runs it on Kalshi's
public trades.

## Questions to expect

- **"How do you know it works?"**
  - Five publicly reported cases, ranked as stories #1, #2, #4, #5 (tied) and #7 of 128 events.
    Two of them (the US strikes on Iran and the Iran ceasefire, both 2026) were added after
    the engine was frozen.
  - The engine's top Maduro alert matches the trading in the CFTC's first event-contract insider complaint
    (436,760 Yes shares).
  - A planted-trade backtest: 82–84% of informed trades land in the top 1%.
  - Every engine change is checked against both.
  - README: *Validation on publicly reported cases*.
- **"False positives?"** Accounts that sold before the news break reach the threshold
  0–3% of the time. Hedges and exits are deducted. A human decides every case, and
  reviewer decisions measure precision per signal.
- **"Has it ever flagged something before the outcome was known?"** Yes, by design: the live
  watch runs on open markets, and every alert is followed up once its market resolves
  (`./grib scorecard`). Replayed on 402 markets never used to design it, the flagged side won 11 of
  19 times against 7.6 implied by the prices paid (one-sided p = 0.035). Modest, real, and growing
  with the live scorecard. Say so plainly.
- **"Sports?"** This is a deliberate blind spot for this method: everyone trades sports
  late. Match-fixing needs other data.
- **"Kalshi already has Solidus."** Yes. Our customers are the second tier of new venues,
  and our product is the analyst's output (the case file), not only detection.
