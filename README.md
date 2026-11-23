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
4. `uv run pytest` — run the tests

More steps are added as milestones land (signals, case files, dashboard).

## How it is organised

| Folder    | What it does |
|-----------|--------------|
| `ingest/` | Polite API clients (disk cache, rate limit, retries) and the loader |
| `schema/` | The venue-agnostic canonical schema and the Polymarket → canonical mapper |
| `signals/`, `scoring/`, `cases/`, `app/`, `eval/` | Later milestones |

**Canonical schema** — every signal reads only these tables, so an operator's CSV can be
mapped in later without changing the engine:

- `accounts(account_id, venue, first_seen_at, metadata_json)`
- `markets(market_id, venue, question, category, opened_at, closes_at, resolved_at, winning_outcome, metadata_json)`
- `trades(trade_id, venue, market_id, account_id, ts, side, outcome, price, size, notional)`

## Data sources

- Polymarket **Data API v2** (`https://data-api.polymarket.com/v2`, public): trades, wallet stats, price history.
- Polymarket **Gamma API** (`https://gamma-api.polymarket.com`, public): market questions, tags, close time, final outcome.

All responses are cached under `data/cache/` and requests are rate-limited (≤4 req/s).
