import math
from datetime import datetime, timedelta

import pytest

from scoring.score import WEIGHTS, score_position, short_account
from signals.base import Buy, PositionContext
from signals.category_prior import CATEGORY_WEIGHTS, category_prior
from signals.fresh_wallet import fresh_single_purpose_wallet
from signals.low_probability import low_probability_conviction
from signals.size_anomaly import size_anomaly
from signals.timing import lead_weight, pre_resolution_timing

CERT = datetime(2026, 11, 1, 12, 0)


def buy(hours_before: float, price: float, notional: float) -> Buy:
    return Buy(CERT - timedelta(hours=hours_before), price, notional / price, notional)


def ctx(buys, **kw) -> PositionContext:
    base = dict(market_id="m1", account_id="0x" + "ab" * 20, outcome="Yes", question="Will X be named CEO?",
                category="personnel", certainty_at=CERT, resolved_at=CERT + timedelta(hours=6),
                market_volume=100_000.0, buys=buys)
    base.update(kw)
    return PositionContext(**base)


# ---- signal 1: timing --------------------------------------------------------------
def test_lead_weight_shape():
    assert lead_weight(-1) == 0
    assert lead_weight(0.5) < lead_weight(5) == 1.0
    assert lead_weight(48) < lead_weight(5)
    assert lead_weight(24 * 30) < lead_weight(100) < lead_weight(48)


def test_timing_cheap_buy_shortly_before_scores_high():
    r = pre_resolution_timing(ctx([buy(6, 0.10, 5_000)]))
    assert r.score == 1.0
    assert r.evidence["share_within_24h"] == 1.0 and r.evidence["share_within_1h"] == 0.0
    assert "72 h" in r.reason


def test_timing_expensive_or_old_buys_score_low():
    assert pre_resolution_timing(ctx([buy(6, 0.95, 5_000)])).score < 0.1      # near-certain favourite
    assert pre_resolution_timing(ctx([buy(24 * 40, 0.10, 5_000)])).score < 0.15  # bought 40 days early
    r = pre_resolution_timing(ctx([buy(24 * 40, 0.10, 5_000)]))
    assert "outside the 72 h window" in r.reason


def test_timing_is_notional_weighted():
    mixed = pre_resolution_timing(ctx([buy(6, 0.10, 9_000), buy(24 * 40, 0.10, 1_000)]))
    assert 0.9 < mixed.score <= 1.0
    assert mixed.evidence["share_within_72h"] == 0.9


# ---- signal 2: low-probability conviction -------------------------------------------
def test_low_probability_scales_with_share_and_surprise():
    small = low_probability_conviction(ctx([buy(6, 0.10, 1_000)]))
    big = low_probability_conviction(ctx([buy(6, 0.10, 10_000)]))
    assert 0 < small.score < big.score < 1
    raw = math.log(10) * 10_000 / 100_000
    assert big.evidence["surprise_weighted_share"] == pytest.approx(raw, abs=1e-4)
    assert big.score == pytest.approx(1 - math.exp(-raw / 0.03), abs=1e-6)
    assert big.evidence["position_payout"] == pytest.approx(100_000)
    assert big.evidence["position_profit"] == pytest.approx(90_000)


def test_low_probability_ignores_favourites():
    r = low_probability_conviction(ctx([buy(6, 0.60, 50_000)]))
    assert r.score == 0 and "not against the market" in r.reason


# ---- signal 3: fresh wallet -----------------------------------------------------------
def test_fresh_wallet_new_single_market_account():
    c = ctx([buy(6, 0.2, 5_000)], account_first_seen_at=CERT - timedelta(days=1),
            account_first_seen_source="venue_join_date", lifetime_markets=1,
            dataset_buy_notional=5_000)
    r = fresh_single_purpose_wallet(c)
    assert r.score > 0.9
    assert r.evidence["markets_traded"] == 1 and r.evidence["share_of_account_buying_in_this_market"] == 1.0


def test_fresh_wallet_old_busy_account_scores_low():
    c = ctx([buy(6, 0.2, 5_000)], account_first_seen_at=CERT - timedelta(days=400),
            account_first_seen_source="venue_join_date", lifetime_markets=500,
            dataset_buy_notional=500_000)
    assert fresh_single_purpose_wallet(c).score < 0.05


def test_fresh_wallet_unknown_join_date_is_downweighted():
    kw = dict(account_first_seen_at=CERT - timedelta(days=1), lifetime_markets=1, dataset_buy_notional=5_000)
    known = fresh_single_purpose_wallet(ctx([buy(6, 0.2, 5_000)], account_first_seen_source="venue_join_date", **kw))
    unknown = fresh_single_purpose_wallet(ctx([buy(6, 0.2, 5_000)], account_first_seen_source="dataset_first_trade", **kw))
    assert unknown.score < known.score
    assert "join date unknown" in unknown.reason


# ---- signal 4: size anomaly -----------------------------------------------------------
def test_size_anomaly_vs_market_and_own_history():
    base = dict(market_log_median=2.0, market_log_mad=0.5, market_percentile=0.999, market_n_positions=800)
    whale = size_anomaly(ctx([buy(6, 0.2, 100_000)], **base))              # z ≈ 4.0
    assert whale.score == pytest.approx(1.0, abs=0.01)
    typical = size_anomaly(ctx([buy(6, 0.2, 100)], **base))
    assert typical.score == 0
    with_hist = size_anomaly(ctx([buy(6, 0.2, 100_000)], other_position_notionals=[100_000] * 5, **base))
    assert with_hist.score == pytest.approx(0.7, abs=0.01)                  # big for market, normal for account
    assert with_hist.evidence["multiple_of_account_typical"] == 1.0


# ---- signal 7: category prior ---------------------------------------------------------
def test_category_prior():
    assert category_prior(ctx([buy(1, .5, 10)], category="personnel")).score == 1.0
    assert category_prior(ctx([buy(1, .5, 10)], category="crypto_price")).score == CATEGORY_WEIGHTS["crypto_price"]
    assert category_prior(ctx([buy(1, .5, 10)], category="made_up")).evidence["category"] == "other"


# ---- combined score -------------------------------------------------------------------
def test_score_is_sum_of_contributions_and_traceable():
    b = buy(6, 0.10, 20_000)
    b = Buy(b.ts, b.price, b.size, b.notional, later_price=0.80)
    c = ctx([b], account_first_seen_at=CERT - timedelta(days=2),
            account_first_seen_source="venue_join_date", lifetime_markets=1, dataset_buy_notional=20_000,
            market_log_median=2.0, market_log_mad=0.5, market_percentile=0.999, market_n_positions=500,
            cohort_winner_peers=10, cohort_loser_peers=0,
            other_long_shots=5, other_long_shot_wins=4, other_long_shot_expected_wins=1.0,
            other_long_shot_variance=0.8)
    sp = score_position(c)
    assert abs(sum(WEIGHTS.values()) - 1) < 1e-9
    assert sp.score == pytest.approx(sum(sp.contributions.values()), abs=0.11)
    for s in sp.signals:
        assert sp.contributions[s.name] == pytest.approx(100 * WEIGHTS[s.name] * s.score, abs=0.01)
    assert sp.score > 85
    assert sp.explanation.startswith("Score ")


def test_hedge_reduces_score():
    kw = dict(market_log_median=2.0, market_log_mad=0.5, market_percentile=0.99, market_n_positions=500)
    plain = score_position(ctx([buy(6, 0.10, 20_000)], **kw))
    hedged = score_position(ctx([buy(6, 0.10, 20_000)], hedge_notional=20_000, **kw))
    assert hedged.contributions["hedge_adjustment"] == -20
    assert hedged.score == pytest.approx(plain.score - 20, abs=0.11)


def test_explanation_never_contains_full_wallet_or_accusation():
    sp = score_position(ctx([buy(6, 0.10, 20_000)]))
    assert "0x" + "ab" * 20 not in sp.explanation
    for word in ("insider", "guilty", "fraud"):
        assert word not in sp.explanation.lower()
    assert short_account("0x" + "ab" * 20) == "0xabab…abab"


# ---- timing vs the market's own rhythm ------------------------------------------------
def test_timing_is_relative_to_market_baseline():
    b = [buy(6, 0.10, 5_000)]
    assert pre_resolution_timing(ctx(b, market_avg_lateness=0.2)).score == 1.0     # market money arrives early
    assert pre_resolution_timing(ctx(b, market_avg_lateness=0.99)).score < 0.05    # sports / 5-min crypto
    mid = pre_resolution_timing(ctx(b, market_avg_lateness=0.85)).score
    assert 0.2 < mid < 0.9
    r = pre_resolution_timing(ctx(b, market_avg_lateness=1.0, market_share_within_72h=1.0))
    assert "no later than the market" in r.reason and "100% of all buying" in r.reason


def test_timing_last_hour_discount_still_applies():
    one_h = pre_resolution_timing(ctx([buy(0.5, 0.10, 5_000)], market_avg_lateness=0.2))
    six_h = pre_resolution_timing(ctx([buy(6, 0.10, 5_000)], market_avg_lateness=0.2))
    assert one_h.score < six_h.score


# ---- coordinated entry ---------------------------------------------------------------
@pytest.mark.parametrize("win,lose,lo,hi", [(0, 0, 0, 0), (1, 0, 0, 0), (4, 0, 0.45, 0.55),
                                            (10, 0, 0.99, 1.0), (40, 30, 0.0, 0.2), (3, 6, 0, 0)])
def test_coordinated_entry_uses_losing_side_as_control(win, lose, lo, hi):
    from signals.coordinated_entry import coordinated_entry
    r = coordinated_entry(ctx([buy(6, 0.1, 5_000)], cohort_winner_peers=win, cohort_loser_peers=lose))
    assert lo <= r.score <= hi


# ---- exits and the starting risk band --------------------------------------------------
def test_exit_reduces_score_and_payout():
    held = ctx([buy(6, 0.10, 5_000)])
    sold = ctx([buy(6, 0.10, 5_000)], sold_size_pre=50_000, sold_notional_pre=20_000)
    assert sold.exited_ratio == 1.0 and held.exited_ratio == 0.0
    assert score_position(sold).contributions["exit_adjustment"] == -30
    assert score_position(sold).score < score_position(held).score
    lp = low_probability_conviction(sold).evidence
    assert lp["position_payout"] == 0 and lp["position_profit"] == 15_000


def test_risk_band_rules():
    from scoring.score import risk_band
    strong3 = {"pre_resolution_timing": 0.9, "low_probability_conviction": 0.8, "size_anomaly": 0.75}
    assert risk_band(70, strong3, 0.0)[0] == "High"
    assert risk_band(60, strong3, 0.0)[0] == "Medium"
    assert risk_band(70, strong3, 0.6)[0] == "Low"                      # sold out before public
    assert risk_band(45, strong3, 0.0)[0] == "Low"                      # below review line
    profile_only = {"fresh_single_purpose_wallet": 1.0, "size_anomaly": 1.0, "category_prior": 1.0}
    assert risk_band(80, profile_only, 0.0)[0] == "Low"


# ---- signal 5: price leadership ------------------------------------------------------
def _leg(hours, price, later, notional=5_000):
    b = buy(hours, price, notional)
    return Buy(b.ts, b.price, b.size, b.notional, later_price=later)


def test_price_leadership_rewards_buys_the_market_then_followed():
    from signals.price_leadership import price_leadership
    led = price_leadership(ctx([_leg(30, 0.50, 0.85)]))          # mid-price buy, market followed
    flat = price_leadership(ctx([_leg(30, 0.50, 0.50)]))
    fell = price_leadership(ctx([_leg(30, 0.50, 0.30)]))
    assert led.score > 0.9 and flat.score == 0 and fell.score == 0
    assert "rose to 0.85" in led.reason
    assert price_leadership(ctx([buy(30, 0.5, 5_000)])).score == 0    # no later price known


# ---- signal 6: long-shot track record ------------------------------------------------
@pytest.mark.parametrize("n,w,exp,var,lo,hi", [
    (0, 0, 0, 0, 0, 0),                 # no record
    (5, 1, 1.0, 0.8, 0, 0),             # one lucky win
    (5, 3, 1.0, 0.8, 0.5, 0.75),        # 3 from 5 at ~0.20
    (20, 5, 4.0, 3.2, 0, 0),            # about what prices implied
])
def test_track_record_is_calibrated_by_prices_paid(n, w, exp, var, lo, hi):
    from signals.track_record import long_shot_track_record
    r = long_shot_track_record(ctx([buy(6, 0.1, 1_000)], other_long_shots=n, other_long_shot_wins=w,
                                   other_long_shot_expected_wins=exp, other_long_shot_variance=var))
    assert lo <= r.score <= hi
