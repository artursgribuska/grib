"""Live alert pages: render with the account's buys and venue record; no wallet address anywhere."""
import json
import re
from datetime import datetime, timedelta

import duckdb
import pandas as pd

from schema.canonical import create_tables
from watch.run_live import ALERTS_DDL

ACCT = "0x" + "ab12" * 10
T0 = datetime(2026, 11, 20)


def _live_db():
    con = duckdb.connect()
    create_tables(con)
    con.execute(ALERTS_DDL)
    con.execute("INSERT INTO markets VALUES ('m1','polymarket','Will <b>X</b> happen?','personnel',?,NULL,NULL,NULL,'{}')", [T0])
    rows = []
    for h in range(48):
        rows.append({"trade_id": f"o{h}", "venue": "polymarket", "market_id": "m1", "account_id": "0x" + "cd" * 20,
                     "ts": T0 + timedelta(hours=h), "side": "buy", "outcome": "No", "price": 0.9 - h / 100,
                     "size": 100.0, "notional": 100 * (0.9 - h / 100)})
    for i, h in enumerate((20, 22, 24)):
        rows.append({"trade_id": f"a{i}", "venue": "polymarket", "market_id": "m1", "account_id": ACCT,
                     "ts": T0 + timedelta(hours=h), "side": "buy", "outcome": "Yes", "price": 0.2, "size": 5000.0,
                     "notional": 1000.0})
    con.register("_t", pd.DataFrame(rows))
    con.execute("INSERT INTO trades SELECT * FROM _t")
    con.execute("INSERT INTO accounts VALUES (?, 'polymarket', ?, ?)",
                [ACCT, T0 - timedelta(days=2), json.dumps({"first_seen_source": "venue_join_date", "lifetime_markets_traded": 2})])
    con.execute("INSERT INTO watch_alerts VALUES ('long_shot_entry','m1','Will <b>X</b> happen?','Yes',?,?,?,3000,0.2,'detail')",
                [ACCT, T0 + timedelta(hours=30), T0 + timedelta(hours=24)])
    return con


def test_alert_pages(tmp_path):
    from app.live_pages import build_all, outcome_prices
    con = _live_db()
    px = outcome_prices(con, "m1", "Yes")
    assert len(px) > 20 and px.p.between(0, 1).all()            # 'No' trades imply the 'Yes' price
    alerts = build_all(con, out_dir=tmp_path)
    assert len(alerts) == 1
    pages = [p for p in tmp_path.glob("*.html") if p.name != "index.html"]
    assert len(pages) == 1 and "ab12" not in pages[0].name
    html = pages[0].read_text()
    assert "<b>X</b>" not in html and "Markets traded, lifetime" in html and "$3,000" in html
    assert html.count('class="mark"') == 3
    for p in tmp_path.glob("*.html"):
        assert not re.search(r"0x[0-9a-fA-F]{40}", p.read_text())
