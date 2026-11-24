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

* **Exits**: the same account's sells of the winning outcome *before* certainty_at.
  A position sold out before the answer was public did not profit from the outcome
  and is penalised in scoring (it stays in the table so the reviewer can see it).

* **Market timing baseline**: how late the market's money arrives in general. Sports
  and short crypto windows trade almost entirely in the final hours, so "bought in the
  last 72 h" means nothing there. See signals/timing.py.

* **Entry cohort**: other low-history accounts that bought a long shot in the same
  market within COHORT_WINDOW_H of this position's first buy, on the winning side vs
  the losing side (the losing side is the control). See signals/coordinated_entry.py.

Tables created (all TEMP-free, so the dashboard can read them):
  market_state, positions_all, candidate_positions, account_history,
  account_profile, market_timing, entry_cohorts
"""

from __future__ import annotations

import duckdb
import pandas as pd

from signals import timing

CERTAINTY_PRICE = 0.95       # winner's implied price that counts as "publicly known"
CERTAINTY_MIN_NOTIONAL = 20  # ignore dust prints when locating certainty
MIN_POSITION_USD = 1_000     # materiality floor for a candidate position

# entry-cohort definition (coordinated entry signal)
COHORT_WINDOW_H = 24         # peers whose first buy is within ± this many hours
COHORT_MIN_USD = 250         # ignore tiny peer positions
COHORT_MAX_PRICE = 0.50      # peers must have bought the side the market thought less likely
LOW_HISTORY_MAX_MARKETS = 5  # "low-history" account: traded at most this many markets ...
LOW_HISTORY_MAX_AGE_DAYS = 14  # ... or joined the venue at most this many days before buying

# wallet funding (optional tables from ingest/funding.py)
FUNDING_HUB_MIN_WALLETS = 8  # a sender funding this many scanned wallets is treated as infrastructure


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
                       AND h.account_id = c.account_id AND NOT h.is_winner), 0) AS hedge_notional,
           coalesce(x.sold_size, 0) AS sold_size_pre, coalesce(x.sold_notional, 0) AS sold_notional_pre
    FROM candidate_positions c
    LEFT JOIN (SELECT w.venue, w.market_id, w.account_id, w.outcome,
                      sum(w.size) AS sold_size, sum(w.notional) AS sold_notional
               FROM trades_w w JOIN market_state s USING (venue, market_id)
               WHERE w.side = 'sell' AND w.ts < s.certainty_at
               GROUP BY 1, 2, 3, 4) x USING (venue, market_id, account_id, outcome)
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

    -- every trading account: how much history it has (for the entry-cohort signal)
    CREATE OR REPLACE TABLE account_profile AS
    SELECT t.venue, t.account_id, count(DISTINCT t.market_id) AS dataset_markets,
           a.first_seen_at,
           json_extract_string(a.metadata_json, '$.first_seen_source') AS first_seen_source,
           TRY_CAST(json_extract(a.metadata_json, '$.lifetime_markets_traded') AS INTEGER) AS lifetime_markets
    FROM trades t LEFT JOIN accounts a USING (venue, account_id)
    GROUP BY t.venue, t.account_id, a.first_seen_at, a.metadata_json;

    CREATE OR REPLACE TABLE peer_pool AS
    SELECT p.venue, p.market_id, p.account_id, p.is_winner, p.buy_notional, p.first_buy_ts
    FROM positions_all p JOIN account_profile ap USING (venue, account_id)
    WHERE p.buy_notional >= {COHORT_MIN_USD}
      AND p.buy_notional / nullif(p.buy_size, 0) <= {COHORT_MAX_PRICE}
      AND (coalesce(ap.lifetime_markets, ap.dataset_markets) <= {LOW_HISTORY_MAX_MARKETS}
           OR (ap.first_seen_source = 'venue_join_date'
               AND p.first_buy_ts - ap.first_seen_at <= INTERVAL {LOW_HISTORY_MAX_AGE_DAYS} DAY));

    CREATE OR REPLACE TABLE entry_cohorts AS
    SELECT c.venue, c.market_id, c.account_id,
           count(q.account_id) FILTER (WHERE q.is_winner) AS winner_peers,
           count(q.account_id) FILTER (WHERE NOT q.is_winner) AS loser_peers,
           coalesce(sum(q.buy_notional) FILTER (WHERE q.is_winner), 0) AS winner_peer_usd,
           list(q.account_id ORDER BY q.first_buy_ts) FILTER (WHERE q.is_winner) AS winner_peer_ids
    FROM candidate_positions c
    LEFT JOIN peer_pool q
      ON q.venue = c.venue AND q.market_id = c.market_id AND q.account_id <> c.account_id
     AND abs(epoch(q.first_buy_ts) - epoch(c.first_buy_ts)) <= {COHORT_WINDOW_H} * 3600
    GROUP BY 1, 2, 3;
    """)
    build_market_timing(con)
    build_funding_links(con)


FUNDING_TABLES = ("wallet_funding_scans", "wallet_funding_transfers", "wallet_funding_senders")


def build_funding_links(con: duckdb.DuckDBPyConnection) -> None:
    """Who shares a funder with whom (from ingest/funding.py; empty tables if it never ran).

    funding_senders  each sender with how many scanned wallets it funded and whether it is
                     infrastructure (a contract, or funding ≥ FUNDING_HUB_MIN_WALLETS wallets —
                     bridges, exchanges, on-ramps), which links nothing
    funding_edges    pairs of wallets funded by the same personal sender, or one funding the other
    cohort_funding   per candidate position: how many of its entry-cohort peers are linked to it
    """
    from ingest.funding import DDL as FUNDING_DDL
    con.execute(FUNDING_DDL)
    con.execute(f"""
    CREATE OR REPLACE TABLE funding_senders AS
    WITH t AS (SELECT DISTINCT account_id, sender FROM wallet_funding_transfers)
    SELECT t.sender, count(*) AS wallets_funded, coalesce(any_value(s.is_contract), false) AS is_contract,
           coalesce(any_value(s.is_contract), false) OR count(*) >= {FUNDING_HUB_MIN_WALLETS} AS infrastructure,
           list(t.account_id ORDER BY t.account_id) AS wallets
    FROM t LEFT JOIN wallet_funding_senders s USING (sender)
    GROUP BY 1;

    CREATE OR REPLACE TABLE funding_edges AS
    WITH t AS (SELECT DISTINCT w.account_id, w.sender FROM wallet_funding_transfers w
               JOIN funding_senders f USING (sender) WHERE NOT f.infrastructure)
    SELECT a.account_id AS a, b.account_id AS b, 'shared_sender' AS kind, a.sender AS via
    FROM t a JOIN t b ON a.sender = b.sender AND a.account_id <> b.account_id
    UNION   -- one trading wallet funded another directly
    SELECT account_id, sender, 'direct', sender FROM t WHERE sender IN (SELECT account_id FROM account_profile)
    UNION
    SELECT sender, account_id, 'direct', sender FROM t WHERE sender IN (SELECT account_id FROM account_profile);

    CREATE OR REPLACE TABLE cohort_funding AS
    SELECT ec.venue, ec.market_id, ec.account_id,
           count(DISTINCT e.b) AS linked_peers,
           (SELECT status FROM wallet_funding_scans s WHERE s.account_id = ec.account_id) AS scan_status
    FROM entry_cohorts ec
    LEFT JOIN funding_edges e ON e.a = ec.account_id AND list_contains(ec.winner_peer_ids, e.b)
    GROUP BY 1, 2, 3;
    """)


def build_market_timing(con: duckdb.DuckDBPyConnection) -> None:
    """Per market: how late does its pre-certainty buying arrive? (same lateness scale
    as the timing signal, so the signal can compare a position against its market)."""
    df = con.execute("""
        SELECT w.venue, w.market_id, w.notional,
               epoch(s.certainty_at) - epoch(w.ts) AS lead_s, epoch(s.certainty_at) - epoch(s.first_trade_ts) AS life_s
        FROM trades_w w JOIN market_state s USING (venue, market_id)
        WHERE w.side = 'buy' AND w.ts < s.certainty_at""").df()
    if df.empty:
        out = pd.DataFrame(columns=["venue", "market_id", "avg_lateness", "share_within_72h", "lifetime_hours"])
    else:
        df["lead_h"] = df.lead_s / 3600
        df["late_n"] = df.notional * timing.lateness_array(df.lead_h.to_numpy())
        df["n72"] = df.notional.where(df.lead_h <= 72, 0.0)
        g = df.groupby(["venue", "market_id"])
        out = pd.DataFrame({
            "avg_lateness": g.late_n.sum() / g.notional.sum(),
            "share_within_72h": g.n72.sum() / g.notional.sum(),
            "lifetime_hours": g.life_s.max() / 3600,
        }).reset_index()
    con.register("mt_df", out)
    con.execute("CREATE OR REPLACE TABLE market_timing AS SELECT * FROM mt_df")
    con.unregister("mt_df")


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
