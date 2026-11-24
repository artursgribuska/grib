"""End-to-end feature + scoring test on a tiny synthetic venue (no network, in-memory DB)."""
import json
from datetime import datetime, timedelta

import duckdb
import pandas as pd
import pytest

from schema.canonical import create_tables
from scoring.run import run

T = datetime(2026, 11, 1, 12, 0)   # resolution time
_n = 0


def tr(market, account, hours_before, outcome, price, usd, side="buy"):
    global _n
    _n += 1
    return dict(trade_id=f"t{_n}", venue="v", market_id=market, account_id=account,
                ts=T - timedelta(hours=hours_before), side=side, outcome=outcome,
                price=price, size=usd / price, notional=usd)


def build_db():
    con = duckdb.connect()
    create_tables(con)
    markets = pd.DataFrame([
        dict(market_id="slow", venue="v", question="Will X resign?", category="personnel",
             opened_at=T - timedelta(days=60), closes_at=T, resolved_at=T, winning_outcome="Yes",
             metadata_json=json.dumps({"event_title": "X resigns by...?"})),
        dict(market_id="fast", venue="v", question="Team A vs. Team B", category="sports",
             opened_at=T - timedelta(hours=5), closes_at=T, resolved_at=T, winning_outcome="Yes",
             metadata_json=json.dumps({})),
    ])
    trades = []
    # slow market: most money arrives weeks early
    for d in range(5, 40):
        trades.append(tr("slow", "early_yes", 24 * d, "Yes", 0.20, 2_000))
        trades.append(tr("slow", "early_no", 24 * d, "No", 0.80, 2_000))
    trades += [
        tr("slow", "insider", 6, "Yes", 0.15, 3_000),                 # holds to the end
        tr("slow", "flipper", 10, "Yes", 0.20, 3_000),                # sells out before public
        tr("slow", "flipper", 3, "Yes", 0.21, 3_150, side="sell"),   # sells all 15,000 shares at about cost
        *[tr("slow", f"peer{i}", 8 + i, "Yes", 0.20, 500) for i in range(4)],   # fresh long-shot buyers
        tr("slow", "late", 1, "Yes", 0.97, 500),                       # outcome becomes public
    ]
    # fast market: everything trades in the final hours
    for h in (4, 3, 2.5, 2):
        trades.append(tr("fast", "crowd_yes", h, "Yes", 0.30, 2_000))
        trades.append(tr("fast", "crowd_no", h, "No", 0.70, 2_000))
    trades += [tr("fast", "punter", 3, "Yes", 0.15, 3_000), tr("fast", "late", 0.5, "Yes", 0.97, 500)]
    accounts = pd.DataFrame([dict(account_id=f"peer{i}", venue="v", first_seen_at=T - timedelta(days=1),
                                  metadata_json=json.dumps({"first_seen_source": "venue_join_date",
                                                            "lifetime_markets_traded": 1})) for i in range(4)])
    for name, df in (("markets", markets), ("trades", pd.DataFrame(trades)), ("accounts", accounts)):
        con.register("df", df)
        con.execute(f"INSERT INTO {name} SELECT * FROM df")
        con.unregister("df")
    return con


@pytest.fixture(scope="module")
def scored():
    con = build_db()
    res = {(s.ctx.market_id, s.ctx.account_id): s for s in run(con)}
    return con, res


def test_market_timing_baseline_separates_slow_and_fast_markets(scored):
    con, _ = scored
    mt = dict(con.execute("SELECT market_id, avg_lateness FROM market_timing").fetchall())
    assert mt["slow"] < 0.3 and mt["fast"] > 0.95


def test_late_buy_counts_only_where_the_market_is_not_late_anyway(scored):
    _, res = scored
    slow = res[("slow", "insider")].contributions["pre_resolution_timing"]
    fast = res[("fast", "punter")].contributions["pre_resolution_timing"]
    assert slow > 15 and fast == 0


def test_position_sold_before_public_is_penalised(scored):
    _, res = scored
    flip, ins = res[("slow", "flipper")], res[("slow", "insider")]
    assert flip.ctx.exited_ratio == pytest.approx(1.0)
    assert flip.contributions["exit_adjustment"] == pytest.approx(-30, abs=2)   # sold at ~cost: (almost) full deduction
    assert ins.contributions["exit_adjustment"] == 0
    assert flip.score < ins.score - 25
    lp = next(s for s in flip.signals if s.name == "low_probability_conviction").evidence
    assert lp["position_payout"] == 0 and lp["share_of_position_sold_before_public"] == 1.0


def test_entry_cohort_counts_fresh_long_shot_buyers_on_each_side(scored):
    _, res = scored
    ins = res[("slow", "insider")].ctx
    # the 4 fresh peers plus the flipper (1 market traded, long-shot buy 4 h apart)
    assert (ins.cohort_winner_peers, ins.cohort_loser_peers) == (5, 0)
    assert res[("slow", "insider")].contributions["coordinated_entry"] > 0


def test_rollups_group_flagged_wallets_by_event(scored):
    con, _ = scored
    ev = con.execute("SELECT event_key FROM alert_events").fetchall()
    assert all(k[0] for k in ev)
    assert con.execute("SELECT count(*) FROM alert_wallets WHERE alerts < 2").fetchone()[0] == 0
