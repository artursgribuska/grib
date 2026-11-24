"""Regression tests for issues found in code review (operator data, news, pipeline, prices)."""
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

from ingest import csv_loader as cl
from tests.test_features import T, build_db


def _operator(tmp_path, bets, outcomes=("A", "B", "C"), winner="A"):
    m = tmp_path / "m.csv"
    pd.DataFrame([dict(market_id="M", question="Who will be the next CEO?", resolved_at=T.isoformat(),
                       winning_outcome=winner)]).to_csv(m, index=False)
    t = tmp_path / "t.csv"
    pd.DataFrame(bets).to_csv(t, index=False)
    con = duckdb.connect()
    cl.load(con, "book", m, t)
    return con


def test_three_way_market_does_not_infer_winner_price_from_losers(tmp_path):
    from scoring.features import build_features
    bets = [dict(market_id="M", account_id=f"c{i}", ts=(T - timedelta(hours=h)).isoformat(), outcome=o,
                 decimal_odds=odds, stake=50) for i, (h, o, odds) in enumerate(
                     [(48, "C", 26.0), (40, "B", 1.6), (30, "A", 9.0), (2, "A", 1.02)])]
    con = _operator(tmp_path, bets)
    build_features(con)
    wp = {o: sorted(round(p, 4) for p in ps if p is not None) or None
          for o, ps in con.execute("SELECT outcome, list(winner_price) FROM trades_w GROUP BY 1").fetchall()}
    assert wp["C"] is None and wp["B"] is None                 # losers say nothing about A's price
    assert wp["A"] == [round(1 / 9.0, 4), round(1 / 1.02, 4)]   # A's own bets carry its price
    # certainty comes from the winner's own price (the 1.02 bet), not from a loser at odds 26
    assert con.execute("SELECT certainty_at FROM market_state").fetchone()[0] == T - timedelta(hours=2)


def test_one_sided_operator_volume_is_not_halved(tmp_path):
    from scoring.features import build_features
    bets = [dict(market_id="M", account_id="c1", ts=(T - timedelta(hours=5)).isoformat(), outcome="A",
                 decimal_odds=5.0, stake=1000),
            dict(market_id="M", account_id="c2", ts=(T - timedelta(hours=4)).isoformat(), outcome="B",
                 decimal_odds=1.25, stake=3000)]
    con = _operator(tmp_path, bets, outcomes=("A", "B"))
    build_features(con)
    assert con.execute("SELECT market_volume FROM market_state").fetchone()[0] == 4000


def test_blank_side_cells_mean_buy(tmp_path):
    bets = [dict(market_id="M", account_id="c1", ts=T.isoformat(), outcome="A", price=0.2, size=100, side=None),
            dict(market_id="M", account_id="c2", ts=T.isoformat(), outcome="A", price=0.3, size=100, side="sell")]
    con = _operator(tmp_path, bets)
    assert sorted(r[0] for r in con.execute("SELECT side FROM trades").fetchall()) == ["buy", "sell"]


def test_position_ids_keep_the_full_market_id():
    from scoring.run import run
    con = build_db()
    run(con)
    pids = [r[0] for r in con.execute("SELECT position_id FROM position_scores").fetchall()]
    assert all(p.split(":")[0] in ("slow", "fast") for p in pids)


def test_news_skips_markets_older_than_the_gdelt_window(monkeypatch):
    from types import SimpleNamespace
    from ingest import news
    m = SimpleNamespace(market_id="x", question="q", winner="Yes", category="personnel",
                        first_flagged_buy=pd.Timestamp(datetime(2025, 1, 1)), certainty_at=pd.Timestamp(datetime(2025, 1, 2)))
    row, arts = news.process_market(client=None, http=None, m=m)     # neither is touched
    assert row["status"] == "outside news archive window" and arts == []


def test_news_without_api_key_is_skipped_not_fatal(monkeypatch):
    from ingest import news
    from cases.writer import MissingApiKey
    from scoring.run import run
    con = build_db()
    run(con)
    monkeypatch.setattr(news, "_client", lambda: (_ for _ in ()).throw(MissingApiKey("no key")))
    assert news.run(con)["skipped"] == "no API key"


def test_buy_in_the_last_minutes_has_no_later_price():
    from scoring.features import build_features, candidate_buys
    con = build_db()
    con.execute("""INSERT INTO trades VALUES ('late_buy', 'v', 'slow', 'insider', ?, 'buy', 'Yes', 0.3, 5000, 1500)""",
                [T - timedelta(hours=1, minutes=3)])            # 3 min before the 0.97 print that makes it public
    build_features(con)
    b = candidate_buys(con)
    row = b[(b.account_id == "insider") & (b.notional == 1500)]
    assert len(row) == 1 and pd.isna(row.later_price.iloc[0])
