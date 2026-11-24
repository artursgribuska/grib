# Integrity Analyst — demo (prediction markets)

An AI compliance analyst for betting operators and prediction-market venues. It watches
trading activity, flags **anomalous activity warranting review** (e.g. large, early,
confident bets on the eventual winner), scores it with explainable signals and writes
a case file. This demo runs on **public Polymarket data only**.

> **Disclaimer:** demo built on public data. Alerts are statistical signals, not legal
> findings, and do not identify or accuse any person. Wallets are pseudonymous.

## Quick start

1. Install [uv](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. `uv sync` (installs Python 3.12 + dependencies)
3. `uv run python -m ingest.run` — downloads ~200 recently resolved markets and all their trades into `data/integrity.duckdb` (≈20 min first time; cached afterwards)
4. `uv run python -m ingest.enrich` — fetches public wallet facts (join date, lifetime markets) for candidate wallets (≈12 min first time)
5. `uv run python -m scoring.run` — scores every candidate position and prints the top 25 alerts
6. Put your Anthropic API key in the environment (`export GRIB_ANTHROPIC_API_KEY=sk-ant-...`, or a `.env` file — see `.env.example`; plain `ANTHROPIC_API_KEY` also works locally, but Claude Code cloud sessions strip it)
7. `uv run python -m cases.run` — writes AI case files for the top 10 alerts into `data/cases/` (`--bundles-only` works without a key)
8. `uv run pytest` — run the tests

More steps are added as milestones land (dashboard).

## Case files

For each alert, `cases/bundle.py` builds an **evidence bundle** (JSON): market facts, account
facts (truncated ID only), the account's trades, the market's price path, every signal's
reason and numbers, market context and the data limitations. The model
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
| Pre-resolution timing | 22 | How much *later than the market's own money* the position was bought, at a low price. A market that trades almost entirely in its final hours (sports, 5-minute crypto) gives no timing signal |
| Low-probability conviction | 22 | Size of bets below 0.30 implied probability, relative to market volume (log(1/price) "surprise") |
| Fresh / single-purpose wallet | 18 | New account, few markets ever traded, most of its money in this market |
| Size anomaly | 13 | Position size vs. this market (robust z-score) and vs. the account's own history |
| Category prior | 12 | How likely a small group knows the answer early (personnel, corporate, government ≫ sports, prices) |
| Coordinated entry | 13 | Other low-history accounts buying the same long shot within ±24 h, compared against the same flow on the losing side |
| Hedge adjustment | −20 | Deducted if the same account also bought the losing side |
| Exit adjustment | −30 | Deducted in proportion to the shares sold back *before* the outcome became public |

`scoring.run` also rolls alerts up **by event** (related markets on the same story, e.g.
several "…by date?" variants) and **by wallet** (accounts flagged in several markets),
in the `alert_events` and `alert_wallets` tables.

Each evidence bundle carries a **starting risk rating** from fixed rules (Low if most of
the position was sold before public, if no behavioural signal is at least supporting, or
if the score is under 50; High at 65+ with three strong signals including a behavioural
one; Medium otherwise). The case writer keeps it unless it names a specific fact in the
bundle that justifies moving one level.

Alerts are **anomalous activity warranting review**, not findings of wrongdoing.

## How it is organised

| Folder    | What it does |
|-----------|--------------|
| `ingest/` | Polite API clients (disk cache, rate limit, retries) and the loader |
| `schema/` | The venue-agnostic canonical schema and the Polymarket → canonical mapper |
| `signals/` | One module per detection signal; each returns a score, a reason and evidence |
| `scoring/` | Feature tables (from canonical tables only) and the weighted score |
| `cases/` | Evidence bundles, the case-file prompt, the LLM writer and the number/language checks |
| `app/`, `eval/` | Later milestones |

**Canonical schema** — every signal reads only these tables, so an operator's CSV can be
mapped in later without changing the engine:

- `accounts(account_id, venue, first_seen_at, metadata_json)`
- `markets(market_id, venue, question, category, opened_at, closes_at, resolved_at, winning_outcome, metadata_json)`
- `trades(trade_id, venue, market_id, account_id, ts, side, outcome, price, size, notional)`

## Data sources

- Polymarket **Data API v2** (`https://data-api.polymarket.com/v2`, public): trades, wallet stats, price history.
- Polymarket **Gamma API** (`https://gamma-api.polymarket.com`, public): market questions, tags, close time, final outcome.

All responses are cached under `data/cache/` and requests are rate-limited (≤4 req/s).
