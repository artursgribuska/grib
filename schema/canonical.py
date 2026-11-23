"""The venue-agnostic canonical schema.

Every detection signal reads ONLY these three tables. Polymarket data is mapped into
them by `schema/polymarket.py`; an operator's bet export will be mapped into the same
tables later (milestone 5). Extra venue-specific detail lives in `metadata_json`.

Conventions
  * timestamps are UTC (TIMESTAMP, naive = UTC)
  * `side` is 'buy' or 'sell' from the account's perspective
  * `outcome` is the label of the outcome traded (e.g. 'Yes', 'No', a team name)
  * `price` is the implied probability paid, 0..1; `size` is units/shares;
    `notional` = price * size in the venue's currency (USD for Polymarket)
"""

import duckdb

DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id     VARCHAR NOT NULL,
    venue          VARCHAR NOT NULL,
    first_seen_at  TIMESTAMP,
    metadata_json  JSON,
    PRIMARY KEY (venue, account_id)
);

CREATE TABLE IF NOT EXISTS markets (
    market_id        VARCHAR NOT NULL,
    venue            VARCHAR NOT NULL,
    question         VARCHAR,
    category         VARCHAR,
    opened_at        TIMESTAMP,
    closes_at        TIMESTAMP,
    resolved_at      TIMESTAMP,
    winning_outcome  VARCHAR,
    metadata_json    JSON,
    PRIMARY KEY (venue, market_id)
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id    VARCHAR NOT NULL,
    venue       VARCHAR NOT NULL,
    market_id   VARCHAR NOT NULL,
    account_id  VARCHAR NOT NULL,
    ts          TIMESTAMP NOT NULL,
    side        VARCHAR NOT NULL,
    outcome     VARCHAR NOT NULL,
    price       DOUBLE NOT NULL,
    size        DOUBLE NOT NULL,
    notional    DOUBLE NOT NULL,
    PRIMARY KEY (venue, trade_id)
);
"""

CANONICAL_COLUMNS = {
    "accounts": ["account_id", "venue", "first_seen_at", "metadata_json"],
    "markets": ["market_id", "venue", "question", "category", "opened_at", "closes_at",
                "resolved_at", "winning_outcome", "metadata_json"],
    "trades": ["trade_id", "venue", "market_id", "account_id", "ts", "side", "outcome",
               "price", "size", "notional"],
}


def create_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(DDL)
