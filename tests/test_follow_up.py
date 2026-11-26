"""Live-watch follow-up: records outcomes once markets resolve and compares wins with implied odds."""
from datetime import datetime

import duckdb

from watch.follow_up import check, excess_wins, report, scorecard
from watch.run_live import ALERTS_DDL

NOW = datetime(2026, 12, 1)


def _db():
    con = duckdb.connect()
    con.execute(ALERTS_DDL)
    rows = [("long_shot_entry", "m1", "Q1?", "Yes", "0x" + "a" * 40, 0.10),
            ("run_up_initiator", "m2", "Q2?", "No", "0x" + "b" * 40, 0.40),
            ("long_shot_entry", "m3", "Q3?", "Yes", "0x" + "c" * 40, 0.15)]
    for k, m, q, o, a, p in rows:
        con.execute("INSERT INTO watch_alerts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [k, m, q, o, a, NOW, NOW, 5000.0, p, "detail"])
    return con


GAMMA = {
    "m1": {"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]', "closedTime": "2026-11-30 10:00:00+00"},
    "m2": {"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]', "closedTime": "2026-11-30 11:00:00+00"},
    "m3": {"closed": False, "outcomes": '["Yes","No"]', "outcomePrices": '["0.2","0.8"]'},
}


def test_check_records_outcomes_and_rechecks_only_open_markets():
    con = _db()
    calls = []
    fetch = lambda mid: calls.append(mid) or GAMMA[mid]
    assert check(con, now=NOW, fetch=fetch) == 2
    assert sorted(calls) == ["m1", "m2", "m3"]
    calls.clear()
    assert check(con, now=NOW, fetch=fetch) == 0
    assert calls == ["m3"]                       # resolved markets are not looked up again
    s = scorecard(con).set_index("question")
    assert s.loc["Q1?", "won"] == True and s.loc["Q2?", "won"] == False and s.won.isna()["Q3?"]


def test_excess_wins_and_report():
    con = _db()
    check(con, now=NOW, fetch=GAMMA.get)
    x = excess_wins(scorecard(con))
    assert x["alerts"] == 3 and x["resolved"] == 2 and x["wins"] == 1
    assert abs(x["expected_wins"] - 0.5) < 1e-9 and x["z"] > 0
    text = report(con)
    assert "won **1 of 2**" in text and "Too few" in text and "lost (Yes)" in text and "| open |" in text
    assert "0x" + "a" * 40 not in text


def test_missing_market_is_skipped():
    con = _db()
    assert check(con, now=NOW, fetch=lambda mid: None) == 0
    assert scorecard(con).won.isna().all()


def test_scorecard_reads_without_outcomes_table_and_read_only(tmp_path):
    db = tmp_path / "live.duckdb"
    con = duckdb.connect(str(db))
    con.execute(ALERTS_DDL)
    con.execute("INSERT INTO watch_alerts VALUES ('k','m','Q?','Yes','acct',?,?,1.0,0.1,'d')", [NOW, NOW])
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    s = scorecard(ro)
    assert len(s) == 1 and s.won.isna().all()


def test_digest_includes_track_record(tmp_path, monkeypatch):
    import watch.run_live as rl
    monkeypatch.setattr(rl, "OUT", tmp_path)
    text = rl.write_digest([], 10, 100, NOW, {"resolved": 4, "wins": 2, "expected_wins": 0.8, "alerts": 6, "z": 1.5})
    assert "Track record" in text and "won 2" in text and "implied 0.8" in text
    assert "Track record" not in rl.write_digest([], 10, 100, NOW, None)


def test_exact_p_matches_hand_calculation():
    from watch.follow_up import exact_p
    assert abs(exact_p([0.5, 0.5], 2) - 0.25) < 1e-12 and abs(exact_p([0.5, 0.5], 1) - 0.75) < 1e-12
    assert exact_p([0.1, 0.2], 0) == 1.0


def test_digest_puts_escalations_first(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    import watch.run_live as rl
    monkeypatch.setattr(rl, "OUT", tmp_path)
    a = NS(kind="long_shot_entry", market_id="m1", outcome="Yes", account_id="0x" + "1" * 40, question="Big?", usd=9000,
           avg_price=0.1, detail="d", at=NOW)
    b = NS(kind="long_shot_entry", market_id="m2", outcome="Yes", account_id="0x" + "2" * 40, question="Small?", usd=100,
           avg_price=0.1, detail="d", at=NOW)
    text = rl.write_digest([a, b], 2, 10, NOW, None, {("long_shot_entry", "m2", "Yes", "0x" + "2" * 40): "escalate",
                                                     ("long_shot_entry", "m1", "Yes", "0x" + "1" * 40): "close"})
    assert text.index("Small?") < text.index("Big?") and "AI suggests: escalate" in text
