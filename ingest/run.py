"""Milestone 1 pipeline: pick ~200 recently resolved markets, load all their trades.

    uv run python -m ingest.run                # default: 200 markets
    uv run python -m ingest.run --markets 50   # smaller, faster
    uv run python -m ingest.run --refresh-listing   # re-ask Gamma for newly resolved markets

Steps
  1. Crawl closed events on Gamma across several tags (newest first).
  2. Keep binary markets that resolved cleanly in the last N days with sensible volume.
  3. Choose a mixed sample with per-category quotas and a per-event cap.
  4. Download every trade for those markets from the Data API v2 (cached, rate-limited).
  5. Write raw tables + canonical tables (accounts, markets, trades) into DuckDB.
  6. Print a data-quality summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

import config
from ingest.http import CachedClient
from ingest.polymarket import DataApiClient, GammaClient
from ingest.quality import print_quality_summary
from schema import polymarket as pm
from schema.canonical import create_tables

log = logging.getLogger("ingest")

RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_polymarket_markets (
    condition_id VARCHAR PRIMARY KEY, event_json JSON, market_json JSON, fetched_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS raw_polymarket_trades (
    condition_id VARCHAR, proxy_wallet VARCHAR, side VARCHAR, outcome VARCHAR,
    outcome_index INTEGER, price DOUBLE, size DOUBLE, "timestamp" BIGINT,
    token_id VARCHAR, transaction_hash VARCHAR);
CREATE TABLE IF NOT EXISTS load_log (
    market_id VARCHAR PRIMARY KEY, trade_rows INTEGER, truncated BOOLEAN, loaded_at TIMESTAMP);
"""
RAW_TRADE_FIELDS = ["proxy_wallet", "side", "outcome", "outcome_index", "price", "size",
                    "timestamp", "token_id", "transaction_hash"]


# --------------------------------------------------------------------------- selection
def discover_candidates(gamma: GammaClient, refresh: bool) -> list[tuple[dict, dict]]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=config.SELECTION["lookback_days"])
    seen: dict[str, tuple[dict, dict]] = {}
    for tag in config.CRAWL_TAGS:
        n_events = 0
        for ev in gamma.iter_closed_events(tag, refresh=refresh):
            ev_closed = pm._parse_ts(ev.get("closedTime"))
            if ev_closed and ev_closed < cutoff:
                break  # sorted newest-closed first, so everything after is older
            n_events += 1
            for m in ev.get("markets") or []:
                cid = m.get("conditionId")
                if cid and cid not in seen and _eligible(m, cutoff):
                    seen[cid] = (m, ev)
        log.info("tag %-12s scanned %4d events, %5d eligible markets so far", tag, n_events, len(seen))
    return list(seen.values())


def _eligible(m: dict, cutoff: datetime) -> bool:
    sel = config.SELECTION
    if not m.get("closed") or pm.winning_outcome(m) is None:
        return False
    if len(pm._json_list(m.get("outcomes"))) != 2:
        return False
    closed = pm._parse_ts(m.get("closedTime"))
    if closed is None or closed < cutoff:
        return False
    vol = float(m.get("volumeNum") or m.get("volume") or 0)
    return sel["min_volume_usd"] <= vol <= sel["max_volume_usd"]


def choose_sample(cands: list[tuple[dict, dict]], n: int, seed: int = 7) -> list[tuple[dict, dict]]:
    """Mixed sample: round-robin across categories, higher-volume markets first,
    at most K markets per event, capped quotas for sports/crypto/price markets."""
    by_cat: dict[str, list] = defaultdict(list)
    for m, ev in cands:
        row = pm.map_market(m, ev)
        by_cat[row["category"]].append((m, ev))
    rng = random.Random(seed)
    for cat in by_cat:
        # mostly volume-ordered, lightly shuffled so we don't only get the top markets
        by_cat[cat].sort(key=lambda x: -float(x[0].get("volumeNum") or 0) * rng.uniform(0.5, 1.5))

    per_event: dict[str, int] = defaultdict(int)
    per_cat: dict[str, int] = defaultdict(int)
    chosen: list = []
    queues = {c: list(v) for c, v in by_cat.items()}
    while len(chosen) < n and any(queues.values()):
        for cat in sorted(queues):
            q = queues[cat]
            while q:
                m, ev = q.pop(0)
                if per_event[ev["id"]] >= config.SELECTION["max_markets_per_event"]:
                    continue
                if per_cat[cat] >= config.CATEGORY_QUOTAS.get(cat, 10**9):
                    q.clear()
                    break
                per_event[ev["id"]] += 1
                per_cat[cat] += 1
                chosen.append((m, ev))
                break
            if len(chosen) >= n:
                break
    return chosen


# --------------------------------------------------------------------------- loading
def fetch_trades(data_api: DataApiClient, cid: str) -> tuple[str, list[dict], bool]:
    cap = config.SELECTION["max_trade_rows_per_market"]
    rows = list(data_api.iter_market_trades(cid, max_rows=cap))
    return cid, rows, len(rows) >= cap


def write_market_batch(con, market_rows: list[dict], raw_markets: list[dict]) -> None:
    ids = [r["market_id"] for r in market_rows]
    con.execute("DELETE FROM markets WHERE venue = ? AND market_id IN (SELECT unnest(?))", [pm.VENUE, ids])
    con.execute("DELETE FROM raw_polymarket_markets WHERE condition_id IN (SELECT unnest(?))", [ids])
    mdf = pd.DataFrame(market_rows)
    con.register("mdf", mdf)
    con.execute("INSERT INTO markets SELECT * FROM mdf")
    con.unregister("mdf")
    rdf = pd.DataFrame(raw_markets)
    con.register("rdf", rdf)
    con.execute("INSERT INTO raw_polymarket_markets SELECT * FROM rdf")
    con.unregister("rdf")


def write_trades(con, cid: str, raw_rows: list[dict], truncated: bool) -> int:
    con.execute("DELETE FROM trades WHERE venue = ? AND market_id = ?", [pm.VENUE, cid])
    con.execute("DELETE FROM raw_polymarket_trades WHERE condition_id = ?", [cid])
    if raw_rows:
        raw = pd.DataFrame([{k: r.get(k) for k in RAW_TRADE_FIELDS} for r in raw_rows])
        raw.insert(0, "condition_id", cid)
        con.register("raw", raw)
        con.execute("INSERT INTO raw_polymarket_trades SELECT * FROM raw")
        con.unregister("raw")
        canon = pd.DataFrame(pm.map_trades(raw_rows, cid))
        if not canon.empty:
            con.register("canon", canon)
            con.execute("INSERT INTO trades SELECT * FROM canon")
            con.unregister("canon")
    con.execute("INSERT OR REPLACE INTO load_log VALUES (?, ?, ?, now())", [cid, len(raw_rows), truncated])
    return len(raw_rows)


def rebuild_accounts(con) -> None:
    """Accounts = every wallet seen in trades. first_seen_at here is the first trade in
    OUR dataset; milestone 2 enriches suspicious wallets with the venue's join date."""
    con.execute("""
        CREATE TEMP TABLE new_accounts AS
        SELECT account_id, venue, min(ts) AS first_seen_at
        FROM trades GROUP BY account_id, venue;
        INSERT INTO accounts
        SELECT n.account_id, n.venue, n.first_seen_at,
               json_object('first_seen_source', 'dataset_first_trade')
        FROM new_accounts n
        ANTI JOIN accounts a ON a.account_id = n.account_id AND a.venue = n.venue;
        UPDATE accounts SET first_seen_at = n.first_seen_at
        FROM new_accounts n
        WHERE accounts.account_id = n.account_id AND accounts.venue = n.venue
          AND json_extract_string(accounts.metadata_json, '$.first_seen_source') = 'dataset_first_trade';
        DROP TABLE new_accounts;
    """)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=int, default=config.SELECTION["target_markets"])
    ap.add_argument("--refresh-listing", action="store_true", help="re-query Gamma for newly resolved markets")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config.DATA_DIR.mkdir(exist_ok=True)
    http = CachedClient()
    gamma, data_api = GammaClient(http), DataApiClient(http)

    log.info("1/4 discovering recently resolved markets on Gamma ...")
    cands = discover_candidates(gamma, refresh=args.refresh_listing)
    sample = choose_sample(cands, args.markets)
    log.info("    %d eligible markets, %d selected", len(cands), len(sample))

    con = duckdb.connect(str(config.DB_PATH))
    create_tables(con)
    con.execute(RAW_DDL)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    write_market_batch(
        con,
        [pm.map_market(m, ev) for m, ev in sample],
        [{"condition_id": m["conditionId"], "event_json": json.dumps({k: v for k, v in ev.items() if k != "markets"}),
          "market_json": json.dumps(m), "fetched_at": now} for m, ev in sample],
    )

    log.info("2/4 downloading trades for %d markets (cached, rate-limited) ...", len(sample))
    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_trades, data_api, m["conditionId"]) for m, _ in sample]
        for i, fut in enumerate(as_completed(futures), 1):
            cid, rows, truncated = fut.result()
            total += write_trades(con, cid, rows, truncated)  # DB writes stay on the main thread
            if i % 10 == 0 or i == len(futures):
                log.info("    %3d/%d markets, %d trade rows (network calls %d, cache hits %d)",
                         i, len(futures), total, http.stats["network_calls"], http.stats["cache_hits"])

    log.info("3/4 building accounts table ...")
    rebuild_accounts(con)
    log.info("4/4 data-quality summary")
    print_quality_summary(con)
    con.close()


if __name__ == "__main__":
    main()
