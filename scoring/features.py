"""Feature tables built from the CANONICAL tables only (venue-agnostic).

Key ideas
---------
* **Public certainty time** (`certainty_at`): official resolution often lags the moment
  the answer is public (a game ends, the oracle settles hours later; an award is
  announced, the market resolves a week later). Trading after that moment is not
  informed trading — it's reading the news. We estimate it from the market itself:
  the first trade after which the winning outcome's implied price never again
  traded below CERTAINTY_PRICE (ignoring dust trades). If that never happens before
  resolution, we fall back to the resolution time.

* **Position**: an account's buys of one outcome in one market *before* certainty_at.
  Candidate positions are winning-outcome positions of at least MIN_POSITION_USD.

Tables created (all TEMP-free, so the dashboard can read them):
  market_state, positions_all, candidate_positions, account_history
"""

from __future__ import annotations

import duckdb

CERTAINTY_PRICE = 0.95       # winner's implied price that counts as "publicly known"
CERTAINTY_MIN_NOTIONAL = 20  # ignore dust prints when locating certainty
MIN_POSITION_USD = 1_000     # materiality floor for a candidate position


def build_features(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE trades_w AS
    SELECT t.*, m.winning_outcome,
           CASE WHEN t.outcome = m.winning_outcome THEN t.price ELSE 1 - t.price END AS winner_price
    FROM trades t JOIN markets m USING (venue, market_id)
    WHERE m.winning_outcome IS NOT NULL AND t.ts <= m.resolved_at;

    CREATE OR REPLACE TABLE market_state AS
    WITH last_unc AS (
        SELECT venue, market_id,
               max(ts) FILTER (WHERE winner_price < {CERTAINTY_PRICE} AND notional >= {CERTAINTY_MIN_NOTIONAL}) AS last_uncertain_ts,
               sum(notional) / 2 AS market_volume,
               count(DISTINCT account_id) AS n_accounts,
               min(ts) AS first_trade_ts
        FROM trades_w GROUP BY 1, 2)
    SELECT m.venue, m.market_id, m.question, m.category, m.winning_outcome, m.resolved_at,
           l.market_volume, l.n_accounts, l.first_trade_ts, l.last_uncertain_ts,
           CASE
             WHEN l.last_uncertain_ts IS NULL THEN l.first_trade_ts   -- never uncertain
             ELSE coalesce((SELECT min(w.ts) FROM trades_w w
                            WHERE w.venue = l.venue AND w.market_id = l.market_id
                              AND w.ts > l.last_uncertain_ts), m.resolved_at)
           END AS certainty_at
    FROM markets m JOIN last_unc l USING (venue, market_id);

    -- every account's pre-certainty buys per (market, outcome): used for size distributions
    CREATE OR REPLACE TABLE positions_all AS
    SELECT w.venue, w.market_id, w.account_id, w.outcome,
           (w.outcome = w.winning_outcome) AS is_winner,
           sum(w.notional) AS buy_notional, sum(w.size) AS buy_size, count(*) AS n_buys,
           min(w.ts) AS first_buy_ts, max(w.ts) AS last_buy_ts
    FROM trades_w w JOIN market_state s USING (venue, market_id)
    WHERE w.side = 'buy' AND w.ts < s.certainty_at
    GROUP BY 1, 2, 3, 4, 5;

    -- distribution of position sizes per market (log10 scale, robust)
    CREATE OR REPLACE TABLE market_size_dist AS
    WITH p AS (SELECT venue, market_id, log10(buy_notional) AS lg FROM positions_all WHERE buy_notional > 0),
         med AS (SELECT venue, market_id, median(lg) AS med_log, count(*) AS n_positions FROM p GROUP BY 1, 2)
    SELECT med.*, (SELECT median(abs(p.lg - med.med_log)) FROM p
                   WHERE p.venue = med.venue AND p.market_id = med.market_id) AS mad_log
    FROM med;

    CREATE OR REPLACE TABLE candidate_positions AS
    SELECT p.*, percent_rank() OVER (PARTITION BY p.venue, p.market_id ORDER BY p.buy_notional) AS _pr
    FROM positions_all p;
    CREATE OR REPLACE TABLE candidate_positions AS
    SELECT c.venue, c.market_id, c.account_id, c.outcome, c.buy_notional, c.buy_size, c.n_buys,
           c.first_buy_ts, c.last_buy_ts, c._pr AS market_percentile,
           coalesce((SELECT sum(h.buy_notional) FROM positions_all h
                     WHERE h.venue = c.venue AND h.market_id = c.market_id
                       AND h.account_id = c.account_id AND NOT h.is_winner), 0) AS hedge_notional
    FROM candidate_positions c
    WHERE c.is_winner AND c.buy_notional >= {MIN_POSITION_USD};

    -- account history inside our dataset (all markets, all outcomes)
    CREATE OR REPLACE TABLE account_history AS
    SELECT venue, account_id,
           count(DISTINCT market_id) AS dataset_markets,
           sum(buy_notional) AS dataset_buy_notional,
           list({{'market_id': market_id, 'notional': buy_notional}}) AS per_market
    FROM positions_all
    WHERE account_id IN (SELECT account_id FROM candidate_positions)
    GROUP BY 1, 2;
    """)


def candidate_accounts(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT account_id FROM candidate_positions ORDER BY 1").fetchall()]


def candidate_buys(con: duckdb.DuckDBPyConnection):
    """Individual buy legs behind each candidate position (for the timing / price signals)."""
    return con.execute("""
        SELECT w.venue, w.market_id, w.account_id, w.ts, w.price, w.size, w.notional
        FROM trades_w w
        JOIN candidate_positions c USING (venue, market_id, account_id, outcome)
        JOIN market_state s USING (venue, market_id)
        WHERE w.side = 'buy' AND w.ts < s.certainty_at
        ORDER BY w.venue, w.market_id, w.account_id, w.ts""").df()
