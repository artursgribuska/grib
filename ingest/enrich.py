"""Enrich candidate accounts with venue-level wallet facts (Polymarket `/v2/user-stats`).

Signals need to know how old a wallet is and how many markets it has traded in its
whole life — not just inside our 200-market sample. Polymarket exposes this publicly
per wallet. We only fetch it for *candidate* wallets (those holding a material
position on a winning outcome), to stay polite to the API.

Results go into the canonical `accounts` table:
  * first_seen_at      <- venue join date when known (else first trade in our data)
  * metadata_json      <- {first_seen_source, lifetime_markets_traded, ...}

    uv run python -m ingest.enrich
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import duckdb

import config
from ingest.http import ApiError, CachedClient
from ingest.polymarket import DataApiClient
from scoring.features import build_features, candidate_accounts

log = logging.getLogger("enrich")


def _fetch(api: DataApiClient, wallet: str):
    try:
        return wallet, api.user_stats(wallet)
    except ApiError as e:
        log.warning("user-stats failed for %s…: %s", wallet[:8], e)
        return wallet, None


def enrich(con: duckdb.DuckDBPyConnection, wallets: list[str], workers: int = 4) -> int:
    api = DataApiClient(CachedClient())
    n_ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (wallet, stats) in enumerate(pool.map(lambda w: _fetch(api, w), wallets), 1):
            if stats:
                join = stats.get("join_date")
                meta = {
                    "first_seen_source": "venue_join_date" if join else "dataset_first_trade",
                    "lifetime_markets_traded": stats.get("trades"),
                    "lifetime_biggest_win_usd": stats.get("biggest_win"),
                    "enriched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                pnl = stats.get("all_time_pnl") or {}
                if pnl:
                    meta["lifetime_volume_usd"] = pnl.get("volume_usdc")
                    meta["lifetime_realized_pnl_usd"] = pnl.get("realized_pnl")
                con.execute(
                    """UPDATE accounts SET
                         first_seen_at = CASE WHEN ? IS NOT NULL THEN to_timestamp(?)::TIMESTAMP ELSE first_seen_at END,
                         metadata_json = json_merge_patch(metadata_json, ?)
                       WHERE venue = 'polymarket' AND account_id = ?""",
                    [join, join, json.dumps(meta), wallet],
                )
                n_ok += 1
            if i % 250 == 0:
                log.info("  %d/%d wallets enriched", i, len(wallets))
    return n_ok


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    con = duckdb.connect(str(config.DB_PATH))
    build_features(con)
    wallets = candidate_accounts(con)
    log.info("enriching %d candidate wallets via /v2/user-stats (cached, rate-limited)", len(wallets))
    n = enrich(con, wallets)
    log.info("done: %d/%d wallets had venue stats", n, len(wallets))
    con.close()


if __name__ == "__main__":
    main()
