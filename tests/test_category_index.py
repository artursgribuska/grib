"""Category index re-scores without the category prior and reports every category."""
from tests.test_features import build_db


def test_category_index_removes_category_points():
    from eval.category_index import behaviour_scores, category_table, report
    from scoring.run import run
    con = build_db()
    run(con)
    ps = behaviour_scores(con)
    assert (ps.behaviour <= ps.score + 1e-9).all()
    t, line = category_table(con)
    cats = set(con.execute("SELECT DISTINCT category FROM markets").df().category)
    assert set(t.category) == cats
    assert (t.alerts <= t.positions).all() and t.share_markets_with_alert.between(0, 1).all()
    t2, line2 = category_table(con, line=1e9)
    assert line2 == 1e9 and t2.alerts.sum() == 0
    assert "Category index" in report(con) and "not findings of wrongdoing" in report(con)
