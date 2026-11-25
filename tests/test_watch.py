"""Live watch: fires on open markets using only past trades; replay gives lead times."""
from datetime import timedelta

import pandas as pd

from tests.test_features import T, build_db
from watch import live


def test_long_shot_entry_fires_only_after_the_buy_and_before_resolution():
    con = build_db()
    before = live.scan(con, T - timedelta(hours=7))
    assert not [a for a in before if a.account_id == "insider"]          # no look-ahead
    after = live.scan(con, T - timedelta(hours=5))
    hit = [a for a in after if a.account_id == "insider" and a.kind == "long_shot_entry"]
    assert len(hit) == 1 and hit[0].outcome == "Yes" and hit[0].avg_price == 0.15
    assert live.scan(con, T + timedelta(hours=1)) == []                   # resolved: nothing to protect


def test_run_up_initiator_detects_who_moved_the_price():
    import duckdb
    from schema.canonical import create_tables
    con = duckdb.connect()
    create_tables(con)
    con.execute("INSERT INTO markets VALUES ('m','v','Will X win?','awards',NULL,NULL,NULL,NULL,'{}')")
    rows = [("a0", "early", -30, 0.04, 200), ("a1", "early", -20, 0.05, 200),
            ("b1", "mover", -2.0, 0.06, 3000), ("b2", "mover", -1.8, 0.20, 4000), ("b3", "mover", -1.6, 0.35, 3000),
            ("c1", "follower", -1.2, 0.60, 900), ("c2", "follower", -1.0, 0.70, 900)]
    T0 = pd.Timestamp("2026-10-10 12:00")
    for tid, acct, h, p, usd in rows:
        con.execute("INSERT INTO trades VALUES (?,'v','m',?,?,'buy','Yes',?,?,?)",
                    [tid, acct, T0 + pd.Timedelta(hours=h), p, usd / p, usd])
    alerts = live.scan(con, T0.to_pydatetime(), lookback_h=6)
    init = [a for a in alerts if a.kind == "run_up_initiator"]
    assert [a.account_id for a in init] == ["mover"]


def test_replay_reports_lead_time_on_the_winner():
    from scoring.features import build_features
    con = build_db()
    build_features(con)
    a = live.replay(con, T - timedelta(days=3), T, step_h=2)
    a = live.case_lead_times(con, a)
    ins = a[(a.account_id == "insider") & (a.kind == "long_shot_entry")]
    assert len(ins) == 1 and bool(ins.on_winner.iloc[0]) and 4 <= ins.hours_before_public.iloc[0] <= 6


def test_drip_buying_is_caught_cumulatively():
    import duckdb
    from schema.canonical import create_tables
    con = duckdb.connect()
    create_tables(con)
    con.execute("INSERT INTO markets VALUES ('m','v','Will X be out?','government_military',NULL,NULL,NULL,NULL,'{}')")
    T0 = pd.Timestamp("2026-01-03 06:00")
    i = 0
    for d in range(10):                                   # a busy market: $60k of ordinary buying a day
        for side_out, p in (("Yes", 0.08), ("No", 0.92)):
            i += 1
            con.execute("INSERT INTO trades VALUES (?,'v','m',?,?,'buy',?,?,?,30000)",
                        [f"o{i}", f"crowd{i}", T0 - pd.Timedelta(days=d, hours=3), side_out, p, 30000 / p])
    for k, h in enumerate((70, 50, 30, 10)):              # four $3,000 chunks: each tiny, $12k in total
        con.execute("INSERT INTO trades VALUES (?,'v','m','drip',?,'buy','Yes',0.07,?,3000)",
                    [f"d{k}", T0 - pd.Timedelta(hours=h), 3000 / 0.07])
    a = live.replay(con, (T0 - pd.Timedelta(days=4)).to_pydatetime(), T0.to_pydatetime(), step_h=6)
    hit = a[(a.account_id == "drip") & (a.kind == "long_shot_entry")]
    assert len(hit) == 1 and hit.usd.iloc[0] >= 10_000     # fired when the running total crossed $10k
