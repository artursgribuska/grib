"""Data-quality summary over the canonical tables. Run alone with:

    uv run python -m ingest.quality
"""

from __future__ import annotations

import duckdb

import config


def _q(con, sql: str):
    return con.execute(sql).fetchall()


def print_quality_summary(con: duckdb.DuckDBPyConnection) -> dict:
    s = {}
    s["markets"], s["markets_unresolved"], s["markets_no_resolved_at"] = _q(con, """
        SELECT count(*), count(*) FILTER (WHERE winning_outcome IS NULL),
               count(*) FILTER (WHERE resolved_at IS NULL) FROM markets""")[0]
    s["trades"], s["accounts_in_trades"], s["min_ts"], s["max_ts"], s["notional"] = _q(con, """
        SELECT count(*), count(DISTINCT account_id), min(ts), max(ts), sum(notional) FROM trades""")[0]
    s["accounts"] = _q(con, "SELECT count(*) FROM accounts")[0][0]
    zero = _q(con, """SELECT count(*) FROM markets m
                      WHERE NOT EXISTS (SELECT 1 FROM trades t WHERE t.market_id = m.market_id)""")[0][0]
    truncated = _q(con, "SELECT count(*) FROM load_log WHERE truncated")[0][0] if _has(con, "load_log") else 0
    bad_price = _q(con, "SELECT count(*) FROM trades WHERE price <= 0 OR price >= 1")[0][0]
    bad_size = _q(con, "SELECT count(*) FROM trades WHERE size <= 0")[0][0]
    dup = _q(con, "SELECT count(*) - count(DISTINCT trade_id) FROM trades")[0][0]
    after_res = _q(con, """SELECT count(*) FROM trades t JOIN markets m USING (market_id)
                           WHERE t.ts > m.resolved_at + INTERVAL 1 HOUR""")[0][0]
    label_mismatch = _q(con, """
        SELECT count(*) FROM trades t JOIN markets m USING (market_id)
        WHERE NOT list_contains(CAST(json_extract(m.metadata_json, '$.outcomes') AS VARCHAR[]), t.outcome)""")[0][0]
    winner_traded = _q(con, """
        SELECT count(*) FROM markets m WHERE EXISTS
          (SELECT 1 FROM trades t WHERE t.market_id = m.market_id AND t.outcome = m.winning_outcome)""")[0][0]
    pre24 = _q(con, """
        SELECT sum(t.notional) FILTER (WHERE t.ts BETWEEN m.resolved_at - INTERVAL 24 HOUR AND m.resolved_at)
               / nullif(sum(t.notional), 0)
        FROM trades t JOIN markets m USING (market_id)""")[0][0]
    cats = _q(con, """
        SELECT m.category, count(DISTINCT m.market_id) AS n_markets, count(t.trade_id) AS n_trades,
               round(sum(t.notional) / 2) AS volume_usd
        FROM markets m LEFT JOIN trades t USING (market_id)
        GROUP BY 1 ORDER BY 2 DESC""")
    sizes = _q(con, """
        SELECT quantile_cont(n, 0.5), max(n) FROM (SELECT market_id, count(*) n FROM trades GROUP BY 1)""")[0]

    line = "-" * 72
    print(line)
    print("DATA-QUALITY SUMMARY (canonical tables)")
    print(line)
    print(f"Markets              {s['markets']:>10,}   (no clean winner: {s['markets_unresolved']}, "
          f"no resolved_at: {s['markets_no_resolved_at']}, zero trades: {zero})")
    print(f"Trade rows           {s['trades']:>10,}   (one row per wallet side of each fill)")
    print(f"Accounts             {s['accounts']:>10,}   ({s['accounts_in_trades']:,} appear in trades)")
    print(f"Matched volume       ${(s['notional'] or 0) / 2:>12,.0f}   (sum of notional / 2: each fill has two sides)")
    print(f"Trade time range     {s['min_ts']}  ->  {s['max_ts']}")
    print(f"Trades per market    median {sizes[0] or 0:,.0f}, max {sizes[1] or 0:,}   "
          f"(markets hitting the row cap: {truncated})")
    print(f"Winner traded        {winner_traded}/{s['markets']} markets have trades on the winning outcome")
    print(f"Share of volume in last 24h before resolution: {100 * (pre24 or 0):.1f}%")
    print("Checks:")
    print(f"  price outside (0,1)          {bad_price:>8,}")
    print(f"  size <= 0                    {bad_size:>8,}")
    print(f"  duplicate trade ids          {dup:>8,}")
    print(f"  outcome label not in market  {label_mismatch:>8,}")
    print(f"  trades >1h after resolution  {after_res:>8,}   (redemptions/cleanup trades; excluded by signals)")
    print("By category:")
    print(f"  {'category':<22}{'markets':>8}{'trades':>12}{'volume $':>16}")
    for c, nm, nt, vol in cats:
        print(f"  {c:<22}{nm:>8}{nt:>12,}{(vol or 0):>16,.0f}")
    print(line)
    s.update(zero_trade_markets=zero, truncated=truncated, bad_price=bad_price, dup=dup,
             label_mismatch=label_mismatch, categories=cats)
    return s


def _has(con, table: str) -> bool:
    return bool(_q(con, f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{table}'")[0][0])


if __name__ == "__main__":
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    print_quality_summary(con)
