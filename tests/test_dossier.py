"""Event dossiers render from the synthetic venue: escaped text, no full wallets, a chart."""
import json
import re

from tests.test_features import build_db


def test_dossiers_build_escaped_without_full_wallets(tmp_path):
    from app.dossier import build_all, slug
    from scoring.run import run
    con = build_db()
    con.execute("UPDATE markets SET question = 'Will <b>x</b> resign?', metadata_json = ? WHERE market_id = 'slow'",
                [json.dumps({"event_title": "Story <i>"})])
    run(con)
    made = build_all(con, top=5, out_dir=tmp_path)
    assert made and (tmp_path / "index.html").exists()
    keys = [ev for ev, *_ in made]
    assert "Story <i>" in keys
    page = (tmp_path / f"{slug('Story <i>')}.html").read_text()
    assert "<svg" in page and "Price path" in page
    assert "<b>x</b>" not in page and "Story <i>" not in page
    for f in tmp_path.glob("*.html"):
        assert not re.search(r"0x[0-9a-fA-F]{40}", f.read_text())
    assert slug("A/B: c?") == slug("A/B: c?") and "/" not in slug("A/B: c?")
