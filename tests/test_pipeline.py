"""Pipeline runs end to end on a throwaway database and records the run."""
import json

import duckdb

import config


def test_pipeline_scores_records_run_and_writes_digest(tmp_path, monkeypatch):
    from tests.test_features import build_db
    db = tmp_path / "p.duckdb"
    src = build_db()
    src.execute(f"ATTACH '{db}' AS out")
    for t in ("markets", "trades", "accounts"):
        src.execute(f"CREATE TABLE out.{t} AS SELECT * FROM {t}")
    src.execute("DETACH out")
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    import pipeline
    assert pipeline.main(["--skip-ingest", "--no-funding", "--no-news", "--no-cases"]) == 0
    con = duckdb.connect(str(db))
    run = con.execute("SELECT run_id, ok, steps, alerts_at_threshold, new_alerts FROM pipeline_runs").fetchone()
    assert run[0] == 1 and run[1] is True and set(json.loads(run[2])) == {"score", "dossiers", "screen", "dashboard"}
    assert run[3] >= 1 and len(json.loads(run[4])) == run[3]          # first run: every alert is new
    con.close()
    digest = (tmp_path / "digest" / "latest.md").read_text()
    assert "run 1" in digest and "New alerts" in digest and "not findings of wrongdoing" in digest
    assert (tmp_path / "dashboard" / "index.html").exists()
    # second run: nothing new
    assert pipeline.main(["--skip-ingest", "--no-funding", "--no-news", "--no-cases"]) == 0
    con = duckdb.connect(str(db))
    assert json.loads(con.execute("SELECT new_alerts FROM pipeline_runs WHERE run_id = 2").fetchone()[0]) == []
