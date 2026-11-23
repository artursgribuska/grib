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
    c = ctx([buy(6, 0.10, 20_000)], account_first_seen_at=CERT - timedelta(days=2),
            account_first_seen_source="venue_join_date", lifetime_markets=1, dataset_buy_notional=20_000,
            market_log_median=2.0, market_log_mad=0.5, market_percentile=0.999, market_n_positions=500)
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
