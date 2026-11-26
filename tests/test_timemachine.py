"""The time machine page builds from a known-cases style database: data embedded safely, no full wallets."""
import json
import re

from tests.test_features import build_db


def test_timemachine_builds_on_synthetic_case(tmp_path):
    from app.timemachine import build
    from scoring.run import run
    con = build_db()
    con.execute("UPDATE markets SET question = 'Will <script>x</script> resign?' WHERE market_id = 'slow'")
    run(con)
    con.execute("""CREATE TABLE known_case_markets AS SELECT 'maduro-2026' AS "case", market_id, question,
                   winning_outcome AS winner, 0 AS trades, false AS truncated FROM markets WHERE market_id = 'slow'""")
    page = build(con, out=tmp_path / "tm.html").read_text()
    m = re.search(r"const DATA = (.*);\nconst NS", page)
    data = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(data) == 1 and data[0]["key"] == "maduro-2026"
    c = data[0]
    assert c["series"] and c["start"] < c["public"] < c["end"]
    assert all(c["start"] <= a["t"] <= c["end"] for a in c["accounts"])
    assert "</script>x" not in page.split("const DATA")[1].split("\nconst NS")[0]   # cannot close the script tag
    assert not re.search(r"0x[0-9a-fA-F]{40}", page)
    assert "__DATA__" not in page
