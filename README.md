# Integrity Analyst — demo (prediction markets)

An AI compliance analyst for betting operators and prediction-market venues. It watches
trading activity, flags **anomalous activity warranting review** (e.g. large, early,
confident bets on the eventual winner), scores it with explainable signals and writes
a case file. This demo runs on **public Polymarket data**, and on an operator's own bet
export through the same engine.

**Does it work?** Run blind on markets behind publicly reported suspected-leak episodes, the
unchanged engine ranked them **#1** (Maduro, January 2026), **#4** (Nobel Peace Prize 2025) and
**#2** (US–Iran ceasefire, April 2026, added after the engine was frozen) and **#18** (Google Year in
Search 2025) among 9,376 positions; its #1 alert matches the trading in the CFTC's first
event-contract insider complaint; on planted trades, 82–84% of
informed buys land in the top 1%. See *Validation* below, `docs/demo.md` for a five-minute
walkthrough and `docs/investor-brief.md` for the business case.

> **Disclaimer:** demo built on public data. Alerts are statistical signals, not legal
> findings, and do not identify or accuse any person. Wallets are pseudonymous.

## Quick start

**Day to day:** `uv run python -m pipeline` runs everything below in one go and is safe to repeat
(e.g. daily): new markets are added, caches are reused, and AI case files are only written for
alerts that are new or whose evidence changed. Each run writes a digest to `data/digest/latest.md`. Then open `data/dashboard/index.html`, review alerts,
and record decisions (see *Reviewing alerts*). Step by step, first time:

1. Install [uv](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. `uv sync` (installs Python 3.12 + dependencies)
3. `uv run python -m ingest.run` — downloads ~200 recently resolved markets and all their trades into `data/integrity.duckdb` (≈20 min first time; cached afterwards)
4. `uv run python -m ingest.enrich` — fetches public wallet facts (join date, lifetime markets) for candidate wallets (≈12 min first time)
5. `uv run python -m scoring.run` — scores every candidate position and prints the top 25 alerts
6. `uv run python -m ingest.funding`, then `uv run python -m scoring.run` again — optional: scans on-chain USDC / pUSD deposits into flagged wallets and their entry-cohort peers (Polygon public RPC, no key; ≈10 min; `--rescan` to redo) so scoring can see shared funders
7. Put your Anthropic API key in the environment (`export GRIB_ANTHROPIC_API_KEY=sk-ant-...`, or a `.env` file — see `.env.example`; plain `ANTHROPIC_API_KEY` also works locally, but Claude Code cloud sessions strip it)
8. `uv run python -m ingest.news` — optional: searches GDELT for news about each alerted market (needs the key; ≈1 min per 10 markets)
9. `uv run python -m cases.run` — writes AI case files for the top 10 alerts into `data/cases/` (`--bundles-only` works without a key)
10. `uv run python -m eval.backtest --markets 200 --seed 1` (and `uv run python -m eval.known_cases`) — plants synthetic trades in an in-memory copy of the data and measures how the engine ranks them (≈10 s)
11. `uv run python -m app.build` — writes a self-contained dashboard to `data/dashboard/index.html` (top 50 alerts with score bars, signal reasons, case files, event/wallet roll-ups and the latest backtest); open it in any browser
12. `uv run pytest` — run the tests

## Your own data (operators)

An operator can run the same engine on its own bet export (milestone 5). Prepare up to three
CSVs — markets, bets (decimal odds + stake, or price + size) and optionally accounts (opening
date) — then load them into a **separate** database and run the pipeline on it:

    uv run python -m ingest.csv_loader --venue mybook --markets markets.csv --trades bets.csv --accounts accounts.csv
    GRIB_DB_PATH=data/mybook.duckdb uv run python -m pipeline --skip-ingest --no-funding

(set `GRIB_DB_PATH` for the loader too). Outputs then go to `data/mybook/` so they never mix
with the public demo. Column definitions are in `ingest/csv_loader.py`; rows that fail checks
(bad timestamps, odds ≤ 1, unknown markets …) are counted and reported, not silently dropped.
`examples/operator/` holds a synthetic sportsbook export (2,021 bets, 6 markets) with one planted
pattern — two day-old accounts backing a 9.0 outsider in a CEO-appointment market hours before
the announcement. The engine ranks those two accounts first (scores 73 and 71) and the case
writer rates both High.

## Reviewing alerts

Each alert on the dashboard has a review box: pick **confirmed**, **dismissed**,
**escalated** or **needs info**, add a note (required to dismiss) and save. Decisions stay
in your browser until you click **Export decisions**, then record them:

    uv run python -m app.review import decisions-2026-11-24.json --by <initials>

or straight from the terminal:

    uv run python -m app.review queue                          # open alerts
    uv run python -m app.review decide CASE-99B29C1E78 needs_info --note "KYC requested" --by ag
    uv run python -m app.review history CASE-99B29C1E78        # audit trail
    uv run python -m app.review stats                          # which signals reviewers confirm

Decisions are append-only (who, when, why, and the score at the time) so the trail survives for
audit. `stats` compares confirmed and dismissed alerts per signal and reports a *lift*; it warns
until there are 20 closed decisions and never changes weights by itself.

## Case files

For each alert, `cases/bundle.py` builds an **evidence bundle** (JSON): market facts, account
facts (truncated ID only), the account's trades, the market's price path, every signal's
reason and numbers, market and event context, funding and news evidence where available, a
rule-based starting risk rating, and the data limitations. The model
(`claude-sonnet-5-5`) sees only this bundle and returns a structured case file: summary,
timeline, evidence per signal, innocent explanations considered, risk rating with
confidence, next steps and open questions.

Two automatic checks run on every draft (`cases/numbers.py`):
- **Number check** — every number in the text must appear in the bundle (rounding and
  0–1 shares written as percentages are allowed). Invented figures are sent back to the
  model once; if they persist the case file is flagged "needs attention".
- **Language check** — no accusatory terms ("insider", "guilty", "fraud"…) and no full wallet addresses.

## How an alert is scored

A *candidate position* is an account's buying of the eventual winner, worth at least
$1,000, done **before the outcome became public**. "Became public" is estimated from the
market itself: the moment after which the winner never again traded below 0.95. This
is often hours or days before the official resolution, and it stops us flagging people
who simply reacted to the news.

Score (0–100) = sum of points from each signal; every point is stored against its signal.

| Signal | Max pts | What it measures |
|---|---|---|
| Pre-resolution timing | 19 | How much *later than the market's own money* the position was bought, at a low price. A market that trades almost entirely in its final hours (sports, 5-minute crypto) gives no timing signal |
| Low-probability conviction | 19 | Size of bets below 0.30 implied probability, relative to market volume (log(1/price) "surprise") |
| Fresh / single-purpose account | 15 | New account, few markets ever traded, most of its money in this market |
| Size anomaly | 11 | Position size vs. this market (robust z-score) and vs. the account's own history |
| Category prior | 10 | How likely a small group knows the answer early (personnel, corporate, government ≫ sports, prices) |
| Coordinated entry | 11 | Other low-history accounts buying the same long shot within ±24 h, compared against the same flow on the losing side; shared funding where scanned |
| Price leadership | 9 | Did the market move toward the winner *after* the account bought, before the outcome was public? Works at any entry price |
| Long-shot track record | 6 | Long shots won in other markets vs. the number the prices paid implied (Σ price) |
| Hedge adjustment | −20 | Deducted if the same account also bought the losing side |
| Exit adjustment | −30 | Deducted in proportion to the shares sold back *before* the outcome became public |

**Wallet funding** (`ingest/funding.py`, optional). Polymarket accounts are proxy wallets
on Polygon. For flagged wallets and the low-history wallets that entered alongside them,
we scan USDC and pUSD (Polymarket USD, the venue's collateral) transfers *into* the wallet
around when it started trading. pUSD minted on deposit comes from the zero address and links nothing. Two wallets funded
by the same personal address, or one funding the other, count as a funding link; a
coordinated-entry score of at least 0.70 follows from one linked peer, 1.0 from two.
Transfers under $50 are ignored: address-poisoning spam sends zero or tiny amounts to
many wallets and would otherwise look like a shared funder. Senders that are contracts or
fund 8+ scanned wallets are treated as infrastructure
(bridges, exchanges, on-ramps) and link nothing. Public RPC nodes only serve recent
history without a key; older wallets are marked `archive_unavailable` (point
`POLYGON_RPC_URL` at an archive node to cover them).

**News timestamps** (`ingest/news.py`, optional, uses the Anthropic key). For each alerted
market, Claude writes a search query, GDELT (a free index of online news) returns articles
from 72 h before the first flagged buy to 6 h after the outcome became public, and Claude marks
headlines that report or clearly signal the winning outcome. The bundle then says whether
relevant news was published *before the account's first buy* — the strongest innocent
explanation — and the starting risk rating drops one level if it was. GDELT misses wires,
social media and official releases and records when it *saw* an article, so "no relevant
article" is weak evidence; case files say so.

`scoring.run` also rolls alerts up **by event** (related markets on the same story, e.g.
several "…by date?" variants) and **by wallet** (accounts flagged in several markets),
in the `alert_events` and `alert_wallets` tables.

Each evidence bundle carries a **starting risk rating** from fixed rules (Low if most of
the position was sold before public, if no behavioural signal is at least supporting, or
if the score is under 50; High at 65+ with three strong signals including a behavioural
one; Medium otherwise). The case writer keeps it unless it names a specific fact in the
bundle that justifies moving one level.

Alerts are **anomalous activity warranting review**, not findings of wrongdoing.

## Backtest

There are no labelled insider cases in public data, so `eval/backtest.py` plants
positions whose nature is known into an in-memory copy of the real data (the database
on disk is untouched) and re-scores everything:

- **informed**: a wallet that joined the day before buys the eventual winner 2–36 h before it became public, at the prevailing price (< 0.60), and holds
- **early**: same wallet and size, bought 10–30 days before public (a long-held view)
- **flipper**: an informed-style buy sold back before the outcome became public

Results over all 200 markets, seeds 1–3 (≈30 planted positions per kind and seed),
for categories where non-public information is plausible, with the current eight signals:

| Planted | Top 1% of ~7,000 positions | At/above review threshold | Median score |
|---|---|---|---|
| informed | 82–84% | 42–60% | 48–51 |
| early | 22–39% | 7–11% | 32–34 |
| flipper | 4–14% | 0–3% | 19–21 |

Adding price leadership and track record (`uv run python -m eval.backtest --compare`) raised
informed trades in the top 1% from 80–82% to 82–90% while pushing long-held "early" trades at
the threshold down from 15–32% to 4–14%: better on both sides, so they were adopted.

Informed trades in sports and 5-minute crypto markets are **not** caught (0% at threshold):
those markets trade almost entirely in their final hours, so timing cannot tell an
informed buyer from the crowd. That is a deliberate trade-off, not a bug. Detection
falls off sharply above an entry price of 0.30 (0% caught at 0.45–0.60). Tapering the
low-price cutoff to 0.60 lifted the at-threshold rate but cut the top-1% rate (81% → 74%)
by also lifting real positions, so it was not adopted.

## Validation on publicly reported cases

Planted trades only show the engine finds what we told it to look for. `eval/known_cases.py`
runs the **unchanged engine** on markets that were the subject of public reports of suspected
trading on non-public information, placed *inside* the ordinary universe so case positions
must out-rank everyday ones. The engine sees trades only — no names, times or wallets.

    uv run python -m eval.known_cases     # ≈15 min first time; report in data/known_cases/report.md

| Case (public reports) | Markets | Best rank of 9,376 | Alerts at threshold |
|---|---|---|---|
| Maduro removed from power, January 2026 — "Maduro out by January 31" | 1 | **1** | 4 |
| 2025 Nobel Peace Prize — winner's price jumped from 3¢ to 75¢ ten hours before the announcement | 1 | **4** | 3 |
| Google Year in Search 2025 — ten "most searched" list events, all markets | 487 | **18** | 1 |
| US–Iran ceasefire, April 2026 — **out of sample**: added after the engine was frozen | 3 | **2** | 17 |

20 of the top 25 alerts come from case markets, which hold 26% of positions. The Iran case was
added after all engine changes, as a test the engine could not have been tuned to; its 17 alerts
also show the cost side: in a heavily anticipated event, reviewers would have many leads to clear. The reports are
allegations and the accounts are pseudonymous: this is evidence about the engine, not about
any person.

**Match with a CFTC complaint.** The CFTC's first insider-trading complaint on event contracts
(SDNY 26-cv-3369, April 2026) alleges the purchase of "more than 436,000 Yes shares" of
"Maduro out by January 31, 2026?" between December 2025 and January 2026. The engine's #1
alert is a position of **436,760 Yes shares** in that market, bought 31 December 2025 –
3 January 2026 for $32,538. Identity cannot be confirmed from on-chain data and we do not try;
market, share count, amount and dates match.

**A prospective flag.** On 24 November 2026, before any of the changes described below and
before we had seen any reporting, the first scoring run put a "Jensen Huang rejoins NVIDIA"
position at #1 of 6,966 and grouped 5 flagged accounts (4 low-history) on that event. Reports
published around then ([Benzinga](https://www.benzinga.com/markets/prediction-markets/26/11/63568662/20000-in-bets-350000-in-profit-suspicious-polymarket-clusters-nailed-jensen-huangs-nvidia-return),
[Cryptopolitan](https://www.cryptopolitan.com/bubblemaps-flags-16-polymarket-accounts-that-won-350k-on-huangs-nvidia-return/))
described clusters of accounts, several funded hours before the news, winning about $350k on
Huang's return. We have not verified those reports beyond the trading itself.

**What the study changed.** The first run missed most of the Nobel case, and the reasons were
engine flaws, now fixed (each checked against the planted-trade backtest, which stayed at parity):

- The leak frenzy made the market's own money "late", so timing scored ~0. A market's lateness
  baseline is now capped at the 90th percentile for markets of its age: a 93-day market doing
  96% of its business in its last 72 hours is itself anomalous.
- The account whose buying moved the price was not credited for it. Price leadership now
  includes *move initiation*: the share of the buying that took the price halfway up the
  market's sharpest pre-public run-up.
- Selling into that run-up at a large gain was penalised as an exit; the waiver applies only to
  move initiators (waiving it for everyone lifted flippers in the backtest).
- Long-shot conviction was measured against total volume, including post-news frenzies that
  dwarf the market beforehand (d4vd: $1.6M before, $6.9M total). It now uses pre-public volume.

**Limits.** Four cases is still small evidence, chosen because they were reported; only the Iran
case was added after the engine was frozen. Where
the public reports describe many modest wins across sibling markets (Google), the engine is
weaker: no single position stands out, and the largest cheap buyer of d4vd
in our data is a very large, diversified trader whose positions look like market-making.

## Live watch: alerts before payout

The engine above reviews markets after they resolve. `watch/live.py` runs on markets that are
**still open**, using only trades up to the moment of the scan, so a venue can hold a withdrawal
before it is paid out. Two patterns, both visible without knowing the winner:

- **Long-shot entry**: a fresh or low-history account's *cumulative* buying of one outcome at
  ≤ 20¢ reaches $10,000 or 5% of the market's buying in the last 7 days (cumulative, so
  drip-buying in small chunks does not slip under the bar).
- **Run-up initiator**: an outcome jumps by ≥ 20 points from ≤ 50¢ within 2 hours, and one
  account did ≥ 40% of the buying that took it halfway up.

Replayed over history with only the data available at each moment (`uv run python -m watch.live --replay`):

| | First alert before the news | Alerts on the eventual winner / on other outcomes |
|---|---|---|
| Maduro, January 2026 | **16.6 hours**: the engine's #1 account (5 days old), once its drip-bought total passed $10k at 7¢ | 2 / 0 |
| Nobel Peace Prize 2025 | **10 hours**: the account that started the run-up, on its first $2,675 at 8¢ | 1 / 0 |
| US–Iran ceasefire, April 2026 (**out of sample**) — "by April 7" / "by April 15" | **248 h** / **158 h**: accounts 0 and 1 days old buying at 11–15¢ | 6 / 2 and 4 / 0 |
| 200 ordinary markets, last 14 days each | 6.4 alerts per 1,000 market-days | alerted long shots won 18% vs 15% implied; run-up initiators 43% vs 30% |

The ordinary-market samples are small (11 and 7 alerts): the run-up edge is early evidence of
signal, the long-shot edge is within noise. An account counts as fresh or low-history from the
venue's own record (join date, lifetime markets) when known; only accounts the venue knows
nothing about fall back to what our sample has seen.

**Running it live:** `uv run python -m watch.run_live` loads the ~150 most active open markets
(sports and crypto excluded), their last 7 days of trades, enriches candidate accounts, scans,
and writes new alerts to `data/live/watch.md` (each alert is recorded once, with the time it was
first raised). Schedule it every few hours.

## How it is organised

| Folder    | What it does |
|-----------|--------------|
| `ingest/` | Polite API clients (disk cache, rate limit, retries) and the loader |
| `schema/` | The venue-agnostic canonical schema and the Polymarket → canonical mapper |
| `signals/` | One module per detection signal; each returns a score, a reason and evidence |
| `scoring/` | Feature tables (from canonical tables only) and the weighted score |
| `eval/`   | Backtest (planted trades), the known-cases study (publicly reported episodes), watch replay |
| `watch/`  | Live watch: alerts on open markets before the winner is known |
| `app/`    | Static HTML dashboard and the analyst review loop (`app/review.py`) |
| `pipeline.py` | One-command incremental run, run log and digest |
| `examples/operator/` | Synthetic sportsbook export for the operator path |
| `docs/` | Demo walkthrough, investor brief, market research notes |
| `cases/` | Evidence bundles, the case-file prompt, the LLM writer and the number/language checks |

**Canonical schema** — every signal reads only these tables, which is how an operator's CSV
(`ingest/csv_loader.py`) runs through the same engine:

- `accounts(account_id, venue, first_seen_at, metadata_json)`
- `markets(market_id, venue, question, category, opened_at, closes_at, resolved_at, winning_outcome, metadata_json)`
- `trades(trade_id, venue, market_id, account_id, ts, side, outcome, price, size, notional)`

## Data sources

- Polymarket **Data API v2** (`https://data-api.polymarket.com/v2`, public): trades, wallet stats, price history.
- Polymarket **Gamma API** (`https://gamma-api.polymarket.com`, public): market questions, tags, close time, final outcome.

- Polygon **public RPC** (`polygon-bor-rpc.publicnode.com`, or `POLYGON_RPC_URL`): USDC / pUSD transfers into wallets (funding).
- **GDELT DOC 2.0** (`api.gdeltproject.org`, public): news articles around alerted markets (covers roughly the last three months).
- **Anthropic API**: case files, and news queries / headline relevance.

All responses are cached under `data/cache/` and requests are rate-limited (≤4 req/s per host;
GDELT 1 per 5 s).
