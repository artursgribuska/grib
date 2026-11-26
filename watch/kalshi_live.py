"""The account-free live watch on Kalshi's open markets.

    uv run python -m watch.kalshi_live                 # ~150 most active open markets, last 7 days of trades
    uv run python -m watch.kalshi_live --every 3       # keep running: a scan every 3 hours

Each run loads the most active open Kalshi markets (sports, crypto and financial-price series
excluded, as everywhere else), their last 7 days of public trades (no accounts) into
data/kalshi_live.duckdb, runs watch/flow.py, and records each alert once with the time it was
first raised. New alerts go to data/kalshi_live/watch.md and, if GRIB_ALERT_WEBHOOK_URL is set,
to the team channel. Alerts are leads for review, not findings.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

import config
from ingest import kalshi
from ingest.http import CachedClient
from schema.canonical import create_tables
from watch import flow

log = logging.getLogger("kalshi_live")

DB = config.DATA_DIR / "kalshi_live.duckdb"
OUT = config.DATA_DIR / "kalshi_live"
WINDOW_DAYS = 7
MIN_VOLUME_24H = 200            # contracts traded in the last 24 h
MAX_MARKET_PAGES = 120

ALERTS_DDL = """CREATE TABLE IF NOT EXISTS flow_alerts (
    market_id VARCHAR, outcome VARCHAR, question VARCHAR, first_alerted_at TIMESTAMP, fired_at TIMESTAMP,
    price_before DOUBLE, price_at DOUBLE, window_buy_usd DOUBLE, one_sided DOUBLE, surge DOUBLE,
    PRIMARY KEY (market_id, outcome))"""


def map_open_market(m: dict, ev: dict) -> dict | None:
    if m.get("market_type") != "binary":
        return None
    row = kalshi.map_market({**m, "result": "yes"}, ev)        # reuse the mapping, then clear the outcome
    row.update(resolved_at=None, winning_outcome=None)
    return row


def open_markets(http: CachedClient, n: int) -> list[tuple[dict, dict]]:
    """The n most active open binary markets outside the skipped categories. Pages the open-market list
    (multi-leg combo markets excluded), keeps those with enough 24 h volume, then looks up each one's event
    for its category and title (cached: an event's category does not change)."""
    active, cursor = [], None
    for _ in range(MAX_MARKET_PAGES):
        page = http.get_json(f"{config.KALSHI_API}/markets",
                             {"status": "open", "limit": 1000, "mve_filter": "exclude", "cursor": cursor}, refresh=True)
        active += [m for m in page.get("markets") or []
                   if m.get("market_type") == "binary" and float(m.get("volume_24h_fp") or 0) >= MIN_VOLUME_24H]
        cursor = page.get("cursor")
        if not cursor:
            break
    active.sort(key=lambda m: -float(m.get("volume_24h_fp") or 0))
    out, events = [], {}
    for m in active:
        et = m.get("event_ticker")
        if et not in events:
            try:
                events[et] = http.get_json(f"{config.KALSHI_API}/events/{et}").get("event") or {}
            except Exception:                  # one missing event must not stop the scan
                events[et] = {}
        ev = events[et]
        if (ev.get("category") or "") in kalshi.SKIP_CATEGORIES or not ev:
            continue
        out.append((m, ev))
        if len(out) >= n:
            break
    return out


def load(con, http: CachedClient, pairs, now: datetime) -> int:
    since = int((now - timedelta(days=WINDOW_DAYS)).replace(tzinfo=timezone.utc).timestamp())
    total = 0
    for m, ev in pairs:
        row = map_open_market(m, ev)
        trades = [x for x in (kalshi.map_trade(t, row["market_id"])
                              for t in kalshi.iter_trades(http, row["market_id"], min_ts=since, refresh=True)) if x]
        con.execute("DELETE FROM trades WHERE market_id = ?", [row["market_id"]])
        con.execute("DELETE FROM markets WHERE market_id = ?", [row["market_id"]])
        con.register("_m", pd.DataFrame([row]))
        con.execute("INSERT INTO markets SELECT * FROM _m")
        con.unregister("_m")
        if trades:
            con.register("_t", pd.DataFrame(trades).drop_duplicates("trade_id"))
            con.execute("INSERT INTO trades SELECT * FROM _t")
            con.unregister("_t")
        total += len(trades)
    return total


def run(n_markets: int = 150, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    create_tables(con)
    con.execute(ALERTS_DDL)
    http = CachedClient()
    pairs = open_markets(http, n_markets)
    n_trades = load(con, http, pairs, now)
    last = con.execute("SELECT max(first_alerted_at) FROM flow_alerts").fetchone()[0]
    since = last - timedelta(hours=1) if last else now - timedelta(hours=24)
    alerts = flow.scan(flow.load(con, [m["ticker"] for m, _ in pairs]), since=since, until=now)
    new = []
    for a in alerts:
        if not con.execute("SELECT 1 FROM flow_alerts WHERE market_id = ? AND outcome = ?", [a.market_id, a.outcome]).fetchone():
            con.execute("INSERT INTO flow_alerts VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [a.market_id, a.outcome, a.question, now, a.at, a.price_before, a.price_at, a.window_buy_usd,
                         a.one_sided, a.surge])
            new.append(a)
    con.close()
    text = digest(new, len(pairs), n_trades, now)
    if new:
        from app.notify import notify
        notify(text)
    return {"markets": len(pairs), "trades": n_trades, "alerts": len(alerts), "new": len(new)}


def digest(new, n_markets: int, n_trades: int, now: datetime) -> str:
    lines = [f"# Kalshi account-free watch — {now:%Y-%m-%d %H:%M} UTC", "",
             f"{n_markets} open markets, {n_trades:,} trades from the last {WINDOW_DAYS} days (no accounts). "
             f"**{len(new)} new alert(s).** Leads for review, not findings.", ""]
    for a in sorted(new, key=lambda x: -x.window_buy_usd):
        lines.append(f"- **flow surge** · {a.question} · '{a.outcome}' rose {a.price_before:.2f} → {a.price_at:.2f} in "
                     f"{flow.WINDOW_H} h · ${a.window_buy_usd:,.0f} bought, {a.one_sided:.0%} of all buying, "
                     f"{a.surge:,.0f}× the usual rate · {a.at:%Y-%m-%d %H:%M} UTC")
    text = "\n".join(lines) + "\n"
    (OUT / "watch.md").write_text(text)
    return text


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=int, default=150)
    ap.add_argument("--every", type=float, default=None, help="repeat every N hours (runs until stopped)")
    a = ap.parse_args(argv)
    while True:
        try:
            log.info("done: %s", json.dumps(run(a.markets)))
            print((OUT / "watch.md").read_text())
        except Exception:
            if not a.every:
                raise
            log.exception("scan failed; retrying at the next interval")
        if not a.every:
            return 0
        time.sleep(a.every * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
