"""Ordinary-market replay summary: counts, implied wins and a z-score; known cases excluded."""
import pandas as pd

from tests.test_features import build_db


def test_summarise_counts_and_significance():
    from eval.watch_volume import p_value, summarise
    a = pd.DataFrame({"on_winner": [True, False, True, False], "avg_price": [0.1, 0.2, 0.1, 0.1],
                      "kind": ["long_shot_entry"] * 3 + ["run_up_initiator"]})
    s = summarise(a, 10)
    assert s["alerts"] == 4 and s["wins"] == 2 and abs(s["expected_wins"] - 0.5) < 1e-9
    assert s["z"] > 0 and 0 < s["p_one_sided"] < 0.5
    assert s["by_kind"]["long_shot_entry"] == {"alerts": 3, "wins": 2, "implied": 0.4}
    assert p_value(0.0) == 0.5 and summarise(a.iloc[:0], 10)["z"] is None


def test_report_runs_and_excludes_known_cases():
    from eval.watch_volume import ordinary_markets, report
    from scoring.run import run
    con = build_db()
    run(con)
    con.execute("CREATE TABLE known_case_markets AS SELECT 'x' AS \"case\", 'slow' AS market_id")
    assert "slow" not in set(ordinary_markets(con).market_id)
    text, res = report(con)
    assert "Live watch on ordinary markets" in text and set(res) == {"watch_scope", "all_markets"}
