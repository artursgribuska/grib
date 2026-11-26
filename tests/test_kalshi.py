"""Kalshi mapping: taker buys at the taker side's price, anonymous account, clean binary results only."""
from ingest.kalshi import ANON, map_market, map_trade

EV = {"title": "Who wins?", "category": "Politics", "event_ticker": "EV", "series_ticker": "S"}


def test_map_market_only_clean_binary():
    m = {"ticker": "T", "title": "Will X win?", "yes_sub_title": "X", "market_type": "binary", "result": "yes",
         "open_time": "2026-10-01T00:00:00Z", "close_time": "2026-11-01T00:00:00Z",
         "settlement_ts": "2026-11-01T00:30:00.5Z"}
    r = map_market(m, EV)
    assert r["winning_outcome"] == "Yes" and r["venue"] == "kalshi" and r["question"] == "Will X win?"
    assert r["resolved_at"].isoformat().startswith("2026-11-01T00:30")
    assert '"trade_rows_per_fill": 1' in r["metadata_json"]
    assert map_market({**m, "result": ""}, EV) is None
    assert map_market({**m, "market_type": "scalar"}, EV) is None


def test_map_trade_uses_taker_side_price():
    t = {"trade_id": "a", "created_time": "2026-10-05T12:00:00.1Z", "taker_side": "no",
         "yes_price_dollars": "0.6100", "no_price_dollars": "0.3900", "count_fp": "10.00"}
    r = map_trade(t, "T")
    assert r["outcome"] == "No" and r["side"] == "buy" and r["account_id"] == ANON
    assert abs(r["price"] - 0.39) < 1e-9 and abs(r["notional"] - 3.9) < 1e-9
    assert map_trade({**t, "taker_side": None}, "T") is None
    assert map_trade({**t, "count_fp": "0"}, "T") is None


def test_deadline_is_scheduled_not_early_close():
    base = {"ticker": "T", "title": "Q?", "market_type": "binary", "result": "no",
            "settlement_ts": "2026-10-20T18:35:26Z"}
    early = map_market({**base, "close_time": "2026-10-20T18:05:23Z", "latest_expiration_time": "2045-01-01T15:00:00Z"}, EV)
    assert early["closes_at"].year == 2045
    sched = map_market({**base, "close_time": "2026-10-01T03:59:00Z", "latest_expiration_time": "2026-10-08T14:00:00Z"}, EV)
    assert sched["closes_at"].isoformat() == "2026-10-01T03:59:00"
