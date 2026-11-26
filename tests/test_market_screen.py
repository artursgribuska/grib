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


def _mini():
    import json
    from datetime import datetime, timedelta
    import duckdb
    import pandas as pd
    from schema.canonical import create_tables
    con = duckdb.connect()
    create_tables(con)
    t0 = datetime(2026, 10, 1)
    meta = json.dumps({"event_title": "Out by?", "trade_rows_per_fill": 1})
    mk = [("A", "Out by Oct 20?", "Yes", t0 + timedelta(days=10), t0 + timedelta(days=19)),
          ("B", "Out by Nov 1?", "Yes", t0 + timedelta(days=11), t0 + timedelta(days=31)),
          ("C", "IPO by Oct 12?", "No", t0 + timedelta(days=11), t0 + timedelta(days=11))]
    con.register("_m", pd.DataFrame([{"market_id": m, "venue": "v", "question": q, "category": "personnel",
                                      "opened_at": t0, "closes_at": close, "resolved_at": res, "winning_outcome": w,
                                      "metadata_json": meta if m != "C" else json.dumps({"trade_rows_per_fill": 1})}
                                     for m, q, w, res, close in mk]))
    con.execute("INSERT INTO markets SELECT * FROM _m")
    rows, i = [], 0
    for m, news_h in (("A", 9 * 24), ("B", 9 * 24 + 20), ("C", 10 * 24)):
        for h in range(0, 10 * 24 + 12, 2):
            ts = t0 + timedelta(hours=h)
            p = 0.97 if h >= news_h else 0.1 + 0.4 * h / (10 * 24)
            out, price = ("Yes", p) if m != "C" else ("No", p)
            rows.append({"trade_id": f"{m}{i}", "venue": "v", "market_id": m, "account_id": f"a{i % 7}", "ts": ts,
                         "side": "buy", "outcome": out, "price": price, "size": 1000.0, "notional": 1000 * price})
            i += 1
    con.register("_t", pd.DataFrame(rows))
    con.execute("INSERT INTO trades SELECT * FROM _t")
    return con


def test_sibling_public_moment_and_deadline_decay():
    from scoring.features import build_features
    from scoring.market_screen import features
    con = _mini()
    build_features(con)
    st = con.execute("SELECT market_id, certainty_at FROM market_state").df().set_index("market_id").certainty_at
    assert st["B"] > st["A"]                                   # B alone would count A's post-news hours
    f = features(con).set_index("market_id")
    assert f.loc["B", "certainty_at"] == st["A"]               # the story's public moment comes from A
    assert bool(f.loc["C", "deadline_decay"]) and f.loc["C", "rise"] == 0
    assert not bool(f.loc["A", "deadline_decay"]) and f.loc["A", "rise"] > 0


def test_screen_pages_render(tmp_path):
    import re
    from scoring.features import build_features
    from app.screen_pages import build_all
    con = _mini()
    con.execute("UPDATE markets SET question = 'Out <b>by</b>?' WHERE market_id = 'A'")
    build_features(con)
    import scoring.market_screen as ms
    old, ms.MIN_PRE_VOLUME = ms.MIN_PRE_VOLUME, 0
    try:
        made = build_all(con, top=5, out_dir=tmp_path)
    finally:
        ms.MIN_PRE_VOLUME = old
    assert len(made) == 3 and (tmp_path / "index.html").exists()
    pages = [f.read_text() for _, f in made]
    assert all("<svg" in p and "What drove the score" in p for p in pages)
    assert not any("<b>by</b>" in p for p in pages)
    assert any("not credited" in p for p in pages)          # market C: deadline decay explained
