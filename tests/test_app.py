"""Dashboard renders from the synthetic venue, never shows full wallets, escapes text."""
import re

from tests.test_features import build_db


def test_dashboard_builds_without_full_wallets(tmp_path):
    from app.build import build
    from scoring.run import run
    con = build_db()
    con.execute("UPDATE markets SET question = 'Will <script>x</script> resign?' WHERE market_id = 'slow'")
    run(con)
    out = build(con, top=20, out=tmp_path / "index.html")
    page = out.read_text()
    assert "Top " in page and "Integrity Analyst" in page
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page
    assert not re.search(r"0x[0-9a-fA-F]{40}", page)
    assert "No case file yet" in page            # no case_files table in this database


def test_queue_caps_alerts_per_event_and_counts_the_rest(tmp_path, monkeypatch):
    import json
    from app import build as b
    from scoring.run import run
    from tests.test_features import build_db
    con = build_db()
    con.execute("UPDATE markets SET metadata_json = ? WHERE market_id = 'slow'", [json.dumps({"event_title": "Story"})])
    run(con)
    monkeypatch.setattr(b, "PER_EVENT", 1)
    n_slow = con.execute("SELECT count(*) FROM position_scores WHERE market_id = 'slow'").fetchone()[0]
    d = b.load(con, top=50)
    shown = d["alerts"][d["alerts"].market_id == "slow"]
    assert n_slow > b.PER_EVENT and len(shown) == b.PER_EVENT
    page = b.build(con, top=50, out=tmp_path / "i.html").read_text()
    assert "more alert" in page and "per event" in page
