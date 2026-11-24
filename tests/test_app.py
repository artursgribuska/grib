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
