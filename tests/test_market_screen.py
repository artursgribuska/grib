"""The market screen uses no account information: scrambling account ids changes nothing."""
from tests.test_features import build_db


def test_screen_ignores_accounts_and_scores_in_range():
    from scoring.features import build_features
    from scoring.market_screen import MIN_PRE_VOLUME, screen
    import scoring.market_screen as ms
    con = build_db()
    build_features(con)
    ms.MIN_PRE_VOLUME = 0
    try:
        a = screen(con)
        con.execute("UPDATE trades SET account_id = 'anon'")
        build_features(con)
        b = screen(con)
    finally:
        ms.MIN_PRE_VOLUME = MIN_PRE_VOLUME
    assert len(a) > 0 and a.score.between(0, 100).all()
    assert a.set_index("market_id").score.round(9).equals(b.set_index("market_id").score.round(9).loc[a.market_id])
    assert {"cheap_flow", "rise", "surge", "one_sided", "flow_score"} <= set(a.columns)
