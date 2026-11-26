"""The point-in-time copy hides the outcome and every trade after the alert."""
from datetime import timedelta
from types import SimpleNamespace

from tests.test_features import T, build_db


def test_point_in_time_hides_the_future():
    from eval.triage_eval import point_in_time
    src = build_db()
    at = T - timedelta(hours=10)
    acct = src.execute("SELECT account_id FROM trades WHERE market_id = 'slow' LIMIT 1").fetchone()[0]
    a = SimpleNamespace(kind="long_shot_entry", market_id="slow", question="Q?", outcome="Yes", account_id=acct, at=at,
                        usd=100.0, avg_price=0.1, detail="d")
    con = point_in_time(src, a)
    assert con.execute("SELECT max(ts) FROM trades").fetchone()[0] <= at
    assert con.execute("SELECT winning_outcome, resolved_at FROM markets").fetchone() == (None, None)
    assert con.execute("SELECT count(DISTINCT market_id) FROM trades").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM watch_alerts").fetchone()[0] == 1
