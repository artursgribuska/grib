"""Load settled Kalshi markets and their public trades into the canonical schema.

Kalshi's public trade feed has no account ids, so every trade is stored under one
anonymous account. The position engine (which needs accounts) does not apply. The market
screen (scoring/market_screen.py), which needs no accounts, does.

    uv run python -m ingest.kalshi --markets 150         # into data/kalshi.duckdb
    GRIB_DB_PATH=data/kalshi.duckdb uv run python -m scoring.market_screen --rebuild

Mapping: each Kalshi trade is a taker buying `taker_side` (Yes or No) at that side's price,
`count` contracts paying $1 each. One row per fill (trade_rows_per_fill = 1). A market's
winner is its `result`; it resolved at `settlement_ts`.

Selection: the most recently settled events, skipping sports, crypto and financial-price
series (the screen is blind to them by design, as the live watch is), keeping binary markets
that settled yes/no with at least MIN_CONTRACTS traded.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

import duckdb
import pandas as pd

import config
from ingest.http import CachedClient
from schema.canonical import create_tables
from schema.categories import categorize

log = logging.getLogger("kalshi")

VENUE = "kalshi"
ANON = "kalshi:anonymous"
SKIP_CATEGORIES = {"Sports", "Crypto", "Financials", "Climate and Weather"}
MIN_CONTRACTS = 20_000
EARLY_CLOSE_DAYS = 10
DB = config.DATA_DIR / "kalshi.duckdb"


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def map_market(m: dict, ev: dict) -> dict | None:
    """Canonical market row, or None if not a cleanly settled binary market."""
    if m.get("market_type") != "binary" or m.get("result") not in ("yes", "no"):
        return None
    title = m.get("title") or ev.get("title") or ""
    sub = m.get("yes_sub_title")
    question = title if not sub or sub in title else f"{title} ({sub})"
    kcat = ev.get("category") or ""
    meta = {"ticker": m.get("ticker"), "event_ticker": ev.get("event_ticker"), "series_ticker": ev.get("series_ticker"),
            "event_title": ev.get("title"), "kalshi_category": kcat, "trade_rows_per_fill": 1,
            "accounts": "not published (all trades under one anonymous account)"}
    close, latest = _ts(m.get("close_time")), _ts(m.get("latest_expiration_time"))
    # closes_at is the scheduled deadline. close_time is the actual close: the deadline itself, or an
    # early close when the outcome became known. Expiry adds a settlement buffer of about a week, so a
    # close more than EARLY_CLOSE_DAYS before the scheduled expiry was an early close.
    deadline = latest if close and latest and (latest - close).days > EARLY_CLOSE_DAYS else close
    return {"market_id": m["ticker"], "venue": VENUE, "question": question,
            "category": categorize(question, [kcat.lower()]),
            "opened_at": _ts(m.get("open_time")), "closes_at": deadline,
            "resolved_at": _ts(m.get("settlement_ts")) or _ts(m.get("close_time")),
            "winning_outcome": "Yes" if m["result"] == "yes" else "No", "metadata_json": json.dumps(meta)}


def map_trade(t: dict, ticker: str) -> dict | None:
    side = (t.get("taker_side") or "").lower()
    if side not in ("yes", "no"):
        return None
    price = float(t.get(f"{side}_price_dollars") or 0)
    size = float(t.get("count_fp") or t.get("count") or 0)
    if not (0 < price < 1) or size <= 0:
        return None
    return {"trade_id": t["trade_id"], "venue": VENUE, "market_id": ticker, "account_id": ANON,
            "ts": _ts(t["created_time"]), "side": "buy", "outcome": side.capitalize(),
            "price": price, "size": size, "notional": price * size}


def iter_settled_events(http: CachedClient, max_pages: int = 50, refresh: bool = False):
    cursor = None
    for _ in range(max_pages):
        page = http.get_json(f"{config.KALSHI_API}/events",
                             {"status": "settled", "with_nested_markets": "true", "limit": 200, "cursor": cursor},
                             refresh=refresh)
        yield from page.get("events") or []
        cursor = page.get("cursor")
        if not cursor:
            return


def iter_trades(http: CachedClient, ticker: str, max_rows: int = 300_000):
    """All trades of a settled market (immutable, so cached)."""
    cursor, n = None, 0
    while n < max_rows:
        page = http.get_json(f"{config.KALSHI_API}/markets/trades",
                             {"ticker": ticker, "limit": 1000, "cursor": cursor})
        rows = page.get("trades") or []
        yield from rows
        n += len(rows)
        cursor = page.get("cursor")
        if not cursor or not rows:
            return


def select_markets(http: CachedClient, n: int, refresh: bool = False) -> list[tuple[dict, dict]]:
    out = []
    for ev in iter_settled_events(http, refresh=refresh):
        if (ev.get("category") or "") in SKIP_CATEGORIES:
            continue
        for m in ev.get("markets") or []:
            if float(m.get("volume_fp") or m.get("volume") or 0) >= MIN_CONTRACTS and map_market(m, ev):
                out.append((m, ev))
        if len(out) >= n:
            break
    return out[:n]


def load(con: duckdb.DuckDBPyConnection, pairs, http: CachedClient) -> int:
    create_tables(con)
    total = 0
    for i, (m, ev) in enumerate(pairs, 1):
        row = map_market(m, ev)
        trades = [x for x in (map_trade(t, row["market_id"]) for t in iter_trades(http, row["market_id"])) if x]
        trades = [t for t in trades if t["ts"] <= row["resolved_at"]]
        con.execute("DELETE FROM trades WHERE venue = ? AND market_id = ?", [VENUE, row["market_id"]])
        con.execute("DELETE FROM markets WHERE venue = ? AND market_id = ?", [VENUE, row["market_id"]])
        con.register("_m", pd.DataFrame([row]))
        con.execute("INSERT INTO markets SELECT * FROM _m")
        con.unregister("_m")
        if trades:
            con.register("_t", pd.DataFrame(trades).drop_duplicates("trade_id"))
            con.execute("INSERT INTO trades SELECT * FROM _t")
            con.unregister("_t")
        total += len(trades)
        if i % 10 == 0:
            log.info("%d/%d markets, %d trades", i, len(pairs), total)
    con.execute("DELETE FROM accounts WHERE venue = ?", [VENUE])
    con.execute("INSERT INTO accounts VALUES (?, ?, NULL, ?)",
                [ANON, VENUE, json.dumps({"note": "Kalshi publishes trades without accounts"})])
    return total


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=int, default=150)
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--refresh", action="store_true", help="re-list settled events (trades stay cached)")
    a = ap.parse_args(argv)
    http = CachedClient()
    pairs = select_markets(http, a.markets, refresh=a.refresh)
    log.info("selected %d settled markets", len(pairs))
    con = duckdb.connect(a.db)
    n = load(con, pairs, http)
    con.close()
    print(f"{len(pairs)} Kalshi markets, {n:,} trades → {a.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
