"""Run the live watch on markets that are open right now.

    uv run python -m watch.run_live                 # ~150 most active open markets, last 7 days of trades
    uv run python -m watch.run_live --markets 50

Each run: pick the most active open markets on Gamma (excluding sports and crypto price
markets, where this method is blind by design), load their recent trades fresh (no cache)
into data/live.duckdb, enrich accounts that could trigger an alert (join date, lifetime
markets), scan, and record alerts. An alert is recorded once, with the time it was first
raised; the digest lists the ones that are new in this run.

Output: data/live/watch.md (new alerts), table watch_alerts in data/live.duckdb.
Alerts are leads for review, not findings; accounts are shown truncated.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

import config
from ingest.http import CachedClient
from ingest.polymarket import DataApiClient
from schema import polymarket as pm
from schema.canonical import create_tables
from watch import live

log = logging.getLogger("watch")

DB = config.DATA_DIR / "live.duckdb"
OUT = config.DATA_DIR / "live"
TAGS = ["politics", "geopolitics", "business", "tech", "economy", "culture", "world", "elections",
        "awards", "science"]
SKIP_CATEGORIES = {"sports", "crypto_price"}
WINDOW_DAYS = 7
MIN_VOLUME_24H = 1_000

ALERTS_DDL = """CREATE TABLE IF NOT EXISTS watch_alerts (
    kind VARCHAR, market_id VARCHAR, question VARCHAR, outcome VARCHAR, account_id VARCHAR,
    first_alerted_at TIMESTAMP, activity_at TIMESTAMP, usd DOUBLE, avg_price DOUBLE, detail VARCHAR,
    PRIMARY KEY (kind, market_id, outcome, account_id))"""


def open_markets(http: CachedClient, n: int) -> list[tuple[dict, dict]]:
    """(market, event) pairs for the n most active open markets in leak-prone categories."""
    seen, out = set(), []
    for tag in TAGS:
        page = http.get_json(f"{config.GAMMA_API}/events/keyset",
                             {"closed": "false", "tag_slug": tag, "order": "volume24hr", "ascending": "false",
                              "limit": 100}, refresh=True)
        for ev in page.get("events") or []:
            for m in ev.get("markets") or []:
                cid = m.get("conditionId")
                if not cid or cid in seen or m.get("closed") or not m.get("active", True):
                    continue
                if float(m.get("volume24hr") or 0) < MIN_VOLUME_24H:
                    continue
                row = pm.map_market(m, ev)
                if row["category"] in SKIP_CATEGORIES:
                    continue
                seen.add(cid)
                out.append((m, ev))
    out.sort(key=lambda me: -float(me[0].get("volume24hr") or 0))
    return out[:n]


def load(con, api: DataApiClient, pairs, now: datetime) -> int:
    cutoff = now - timedelta(days=WINDOW_DAYS)
    total = 0
    for m, ev in pairs:
        row = pm.map_market(m, ev)
        row["resolved_at"], row["winning_outcome"] = None, None     # open: no outcome yet
        raw = []
        for r in api.iter_market_trades(row["market_id"], max_rows=200_000, refresh=True):
            if datetime.fromtimestamp(int(r.get("timestamp") or 0), timezone.utc).replace(tzinfo=None) < cutoff:
                break                                                   # newest first: the rest is older
            raw.append(r)
        trades = pm.map_trades(raw, row["market_id"])
        con.execute("DELETE FROM markets WHERE market_id = ?", [row["market_id"]])
        con.execute("DELETE FROM trades WHERE market_id = ?", [row["market_id"]])
        con.register("_m", pd.DataFrame([row]))
        con.execute("INSERT INTO markets SELECT * FROM _m")
        con.unregister("_m")
        if trades:
            con.register("_t", pd.DataFrame(trades))
            con.execute("INSERT INTO trades SELECT * FROM _t")
            con.unregister("_t")
        total += len(trades)
    return total


def candidates(con) -> list[str]:
    """Accounts that could trigger an alert: a cheap-side position of ≥ $1,000, or buying in a run-up."""
    return [r[0] for r in con.execute(f"""
        SELECT account_id FROM trades WHERE side = 'buy'
        GROUP BY account_id, market_id, outcome
        HAVING sum(notional) >= 1000 AND sum(notional) / sum(size) <= {max(live.ENTRY_MAX_PRICE, live.RUN_MAX_START)}
        """).fetchall()]


def run(n_markets: int = 150, now: datetime | None = None) -> dict:
    from ingest.enrich import enrich
    from ingest.run import rebuild_accounts
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    create_tables(con)
    con.execute(ALERTS_DDL)
    http = CachedClient()
    pairs = open_markets(http, n_markets)
    n_trades = load(con, DataApiClient(http), pairs, now)
    rebuild_accounts(con)
    wallets = sorted(set(candidates(con)))
    enrich(con, wallets)
    last = con.execute("SELECT max(first_alerted_at) FROM watch_alerts").fetchone()[0]
    lookback = (now - last).total_seconds() / 3600 + 1 if last else WINDOW_DAYS * 24
    alerts = live.scan(con, now, lookback_h=min(lookback, WINDOW_DAYS * 24))
    new = []
    for a in alerts:
        exists = con.execute("SELECT 1 FROM watch_alerts WHERE kind=? AND market_id=? AND outcome=? AND account_id=?",
                             [a.kind, a.market_id, a.outcome, a.account_id]).fetchone()
        if not exists:
            con.execute("INSERT INTO watch_alerts VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [a.kind, a.market_id, a.question, a.outcome, a.account_id, now, a.at, a.usd, a.avg_price, a.detail])
            new.append(a)
    con.close()
    write_digest(new, len(pairs), n_trades, now)
    return {"markets": len(pairs), "trades": n_trades, "enriched": len(wallets), "alerts": len(alerts), "new": len(new)}


def write_digest(new, n_markets: int, n_trades: int, now: datetime) -> None:
    from scoring.score import short_account
    lines = [f"# Live watch — {now:%Y-%m-%d %H:%M} UTC", "",
             f"{n_markets} open markets, {n_trades:,} trades from the last {WINDOW_DAYS} days. "
             f"**{len(new)} new alert(s).** Leads for review, not findings.", ""]
    for a in sorted(new, key=lambda x: -x.usd):
        lines.append(f"- **{a.kind.replace('_', ' ')}** · {a.question} · '{a.outcome}' · {short_account(a.account_id)} · "
                     f"${a.usd:,.0f} at {a.avg_price:.2f} · {a.detail} · last activity {a.at:%Y-%m-%d %H:%M} UTC")
    (OUT / "watch.md").write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=int, default=150)
    a = ap.parse_args(argv)
    res = run(a.markets)
    log.info("done: %s", json.dumps(res))
    print((OUT / "watch.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
