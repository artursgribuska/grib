"""Backtest planting and stale case-file pruning, on the synthetic venue from test_features."""
import json

import pytest

from tests.test_features import build_db


def test_backtest_plants_and_ranks_known_positions():
    from eval.backtest import evaluate, plant, summarise
    con = build_db()
    planted = plant(con, n_markets=2, seed=1)
    assert set(planted.kind) >= {"informed", "flipper"}
    assert "early" in set(planted[planted.market_id == "slow"].kind)       # slow market lives long enough
    res = evaluate(con, planted)
    got = res.set_index(["market_id", "kind"]).score
    assert got[("slow", "informed")] > got[("slow", "flipper")]
    assert got[("slow", "informed")] > got[("slow", "early")]
    assert "BACKTEST" in summarise(res)


def test_prune_stale_drops_rows_whose_score_changed(tmp_path, monkeypatch):
    import cases.run as cr
    from scoring.run import run
    con = build_db()
    run(con)
    con.execute(cr.DDL)
    pid, score = con.execute("SELECT position_id, score FROM position_scores ORDER BY score DESC LIMIT 1").fetchone()
    row = lambda p, total: [p, None, "m", "Low", "Low", True, "[]", "[]", 1, "{}",
                            json.dumps({"score": {"total": total}}), "{}", ""]
    con.execute("INSERT INTO case_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row(pid, round(score, 1)))
    con.execute("INSERT INTO case_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row("gone:0x1", 12.3))
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path)
    (tmp_path / "01_CASE-OLD0000000.md").write_text("old")
    (tmp_path / "07_CASE-KEEP000000.md").write_text("beyond --top")
    n_files, n_rows = cr.prune_stale(con, [pid], top=1)
    assert (n_files, n_rows) == (1, 1)
    assert con.execute("SELECT position_id FROM case_files").fetchall() == [(pid,)]
    assert (tmp_path / "07_CASE-KEEP000000.md").exists()
