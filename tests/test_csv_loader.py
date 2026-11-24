"""Operator CSV loader: odds conversion, validation, and an end-to-end score on the example export."""
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from ingest import csv_loader as cl

EX = Path(__file__).resolve().parent.parent / "examples" / "operator"


def _write(tmp_path, name, rows):
    p = tmp_path / name
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_odds_and_stake_convert_to_price_size_notional(tmp_path):
    m = _write(tmp_path, "m.csv", [dict(market_id="A", question="Who will be the next CEO?", resolved_at="2026-10-01T12:00Z",
                                        winning_outcome="X")])
    t = _write(tmp_path, "t.csv", [dict(market_id="A", account_id="c1", ts="2026-10-01T06:00:00", outcome="X",
                                        decimal_odds=5.0, stake=100)])
    con = duckdb.connect()
    rep = cl.load(con, "book", m, t)
    price, size, notional, side = con.execute("SELECT price, size, notional, side FROM trades").fetchone()
    assert (price, size, notional, side) == (0.2, 500.0, 100.0, "buy")
    assert con.execute("SELECT category FROM markets").fetchone()[0] == "personnel"     # guessed from question
    assert rep.trades == 1 and not rep.rejected


def test_bad_rows_are_rejected_with_reasons(tmp_path):
    m = _write(tmp_path, "m.csv", [dict(market_id="A", question="q", resolved_at="2026-10-01", winning_outcome="X",
                                        category="not_a_category"),
                                   dict(market_id="B", question="q", resolved_at="", winning_outcome="X")])
    t = _write(tmp_path, "t.csv", [
        dict(market_id="A", account_id="c1", ts="2026-09-30T10:00", outcome="X", price=0.4, size=10),
        dict(market_id="A", account_id="c2", ts="yesterday", outcome="X", price=0.4, size=10),
        dict(market_id="A", account_id="c3", ts="2026-09-30T10:00", outcome="X", price=1.4, size=10),
        dict(market_id="A", account_id="c4", ts="2026-09-30T10:00", outcome="X", price=0.4, size=-1),
        dict(market_id="Z", account_id="c5", ts="2026-09-30T10:00", outcome="X", price=0.4, size=10),
    ])
    rep = cl.load(duckdb.connect(), "book", m, t)
    assert rep.markets == 1 and rep.trades == 1
    for reason in ("unparseable timestamp", "price outside", "size / stake", "market not in markets file",
                   "valid resolved_at", "unknown category"):
        assert any(reason in k for k in rep.rejected), reason


def test_missing_columns_fail_clearly(tmp_path):
    m = _write(tmp_path, "m.csv", [dict(market_id="A", question="q")])
    t = _write(tmp_path, "t.csv", [dict(market_id="A")])
    with pytest.raises(ValueError, match="resolved_at"):
        cl.load(duckdb.connect(), "book", m, t)


def test_reload_replaces_rather_than_duplicates(tmp_path):
    con = duckdb.connect()
    for _ in range(2):
        cl.load(con, "examplebook", EX / "markets.csv", EX / "bets.csv", EX / "accounts.csv")
    assert con.execute("SELECT count(*) FROM trades").fetchone()[0] == len(pd.read_csv(EX / "bets.csv"))


def test_example_export_flags_the_planted_accounts():
    from scoring.run import run
    con = duckdb.connect()
    cl.load(con, "examplebook", EX / "markets.csv", EX / "bets.csv", EX / "accounts.csv")
    top = [s.ctx.account_id for s in run(con)[:2]]
    assert set(top) == {"C90001", "C90002"}
