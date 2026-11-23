import json
from datetime import datetime

import duckdb

from schema import polymarket as pm
from schema.canonical import CANONICAL_COLUMNS, create_tables
from schema.categories import CATEGORIES, categorize

MARKET = {
    "id": "898409",
    "question": "Will Lando Norris be the 2026 F1 Drivers' Champion?",
    "conditionId": "0xabc",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0", "1"]',
    "clobTokenIds": '["111", "222"]',
    "startDate": "2026-01-02T03:04:05Z",
    "endDate": "2027-04-01T03:59:00Z",
    "closedTime": "2026-10-26 01:07:14.220608+00",
    "volumeNum": 12345.6,
    "closed": True,
}
EVENT = {"id": "100371", "title": "F1 Drivers' Champion", "tags": [{"slug": "sports"}, {"slug": "f1"}]}


def test_winning_outcome_clean_and_unclean():
    assert pm.winning_outcome(MARKET) == "No"
    assert pm.winning_outcome({**MARKET, "outcomePrices": '["0.5", "0.5"]'}) is None  # split / voided
    assert pm.winning_outcome({**MARKET, "outcomePrices": '["0.97", "0.03"]'}) is None  # not resolved
    assert pm.winning_outcome({**MARKET, "outcomePrices": None}) is None


def test_parse_ts_handles_gamma_formats():
    assert pm._parse_ts("2026-11-23T09:51:34Z") == datetime(2026, 11, 23, 9, 51, 34)
    assert pm._parse_ts("2026-11-23 22:29:51.069289+00") == datetime(2026, 11, 23, 22, 29, 51, 69289)
    assert pm._parse_ts(0) == datetime(1970, 1, 1)
    assert pm._parse_ts("not a date") is None


def test_map_market():
    row = pm.map_market(MARKET, EVENT)
    assert list(row) == CANONICAL_COLUMNS["markets"]
    assert row["market_id"] == "0xabc"
    assert row["winning_outcome"] == "No"
    assert row["category"] == "sports"
    assert row["resolved_at"] == datetime(2026, 10, 26, 1, 7, 14, 220608)
    meta = json.loads(row["metadata_json"])
    assert meta["token_ids"] == ["111", "222"] and meta["tags"] == ["sports", "f1"]


def _raw(**kw):
    base = {"proxy_wallet": "0xAAA", "side": "BUY", "outcome": "Yes", "outcome_index": 0,
            "price": 0.2, "size": 100.0, "timestamp": 1792863266, "token_id": "111",
            "transaction_hash": "0xt1", "name": "Real Name", "bio": "hello"}
    return {**base, **kw}


def test_map_trades_ids_units_and_privacy():
    rows = pm.map_trades([_raw(), _raw(), _raw(side="SELL", proxy_wallet="0xBBB")], "0xabc")
    assert len(rows) == 3
    assert list(rows[0]) == CANONICAL_COLUMNS["trades"]
    # identical fills in one tx get distinct but stable ids
    assert rows[0]["trade_id"] != rows[1]["trade_id"]
    assert rows[0]["trade_id"] == pm.map_trades([_raw()], "0xabc")[0]["trade_id"]
    assert rows[0]["account_id"] == "0xaaa"          # lower-cased
    assert rows[0]["side"] == "buy" and rows[2]["side"] == "sell"
    assert abs(rows[0]["notional"] - 20.0) < 1e-9    # price * shares
    # profile fields are never carried into the canonical rows
    assert not any("Real Name" in str(v) or "hello" in str(v) for r in rows for v in r.values())


def test_map_trades_skips_malformed():
    assert pm.map_trades([_raw(price=None), _raw(side="?"), _raw(proxy_wallet="")], "m") == []


def test_canonical_tables_accept_mapped_rows():
    con = duckdb.connect()
    create_tables(con)
    import pandas as pd
    con.register("m", pd.DataFrame([pm.map_market(MARKET, EVENT)]))
    con.execute("INSERT INTO markets SELECT * FROM m")
    con.register("t", pd.DataFrame(pm.map_trades([_raw()], "0xabc")))
    con.execute("INSERT INTO trades SELECT * FROM t")
    assert con.execute("SELECT count(*) FROM trades JOIN markets USING (market_id)").fetchone()[0] == 1


def test_categories():
    cases = [
        ("Will Lisa Su be the next CEO of NVIDIA?", ["nvidia", "big-tech"], "personnel"),
        ("Will Trump nominate Kevin Warsh as the next Fed chair?", ["politics", "fed"], "personnel"),
        ("Bitcoin Up or Down - November 23, 8:45AM-9:00AM ET", ["crypto-prices"], "crypto_price"),
        ("Will the price of Bitcoin be less than $56,000 on November 23?", ["crypto"], "crypto_price"),
        ("Spread: Broncos (-10.5)", ["sports", "nfl"], "sports"),
        ("Will Michael Olise finish 2nd in the 2026 Ballon d'Or?", ["awards", "sports"], "awards_entertainment"),
        ("Will Elon Musk post 115-139 tweets from November 21 to November 23, 2026?", ["pop-culture"], "public_metric"),
        ("Saudi Arabia military action against Yemen on September 27?", ["geopolitics"], "government_military"),
        ("Will Core CPI MoM be 0.1% in October?", ["economy", "cpi"], "economics_data"),
        ("Will Apple release a new product line before 2027?", ["tech", "business"], "corporate"),
        ("LAPTOP listed on Coinbase within 30 days of TGE?", ["crypto", "politics"], "corporate"),
        ("Will Flávio Bolsonaro win the second round of the 2026 Brazilian presidential election?",
         ["politics", "elections"], "elections"),
    ]
    for q, tags, expected in cases:
        assert categorize(q, tags) == expected, (q, categorize(q, tags))
    assert categorize("", []) == "other"
    assert set(c for *_, c in cases) <= set(CATEGORIES)
