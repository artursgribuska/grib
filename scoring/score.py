"""Combine signal outputs into a 0–100 alert score with per-signal contributions.

Transparent weighted rule (no ML until a backtest exists):

    score = Σ  100 × weight_k × signal_k          (each signal is 0..1, weights sum to 1)
            − hedge adjustment                     (up to −20 if the account also bought the loser)
            − exit adjustment                      (up to −30 if it sold out before the outcome was public)

Every point of the score is stored against the signal that produced it, so the
dashboard and case file can say exactly why an alert scored what it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from signals import ALL_SIGNALS
from signals.base import PositionContext, SignalResult, money

WEIGHTS = {
    "pre_resolution_timing": 0.19,
    "low_probability_conviction": 0.19,
    "fresh_single_purpose_wallet": 0.15,
    "size_anomaly": 0.11,
    "category_prior": 0.10,
    "coordinated_entry": 0.11,
    "price_leadership": 0.09,
    "long_shot_track_record": 0.06,
}
HEDGE_MAX_PENALTY = 20.0
EXIT_MAX_PENALTY = 30.0    # a position sold out before the answer was public did not profit from it
ALERT_THRESHOLD = 50.0     # "warrants review" line used by the case-file step

LABELS = {
    "pre_resolution_timing": "Pre-resolution timing",
    "low_probability_conviction": "Low-probability conviction",
    "fresh_single_purpose_wallet": "Fresh / single-purpose account",
    "size_anomaly": "Size anomaly",
    "category_prior": "Category prior",
    "coordinated_entry": "Coordinated entry",
    "price_leadership": "Price leadership",
    "long_shot_track_record": "Long-shot track record",
    "hedge_adjustment": "Hedge adjustment",
    "exit_adjustment": "Exit adjustment",
}
ADJUSTMENTS = ("hedge_adjustment", "exit_adjustment")
# behavioural signals describe the trading itself; the rest describe the account/market
BEHAVIOURAL = ("pre_resolution_timing", "low_probability_conviction", "coordinated_entry", "price_leadership",
               "long_shot_track_record")
STRONG_AT, SUPPORTING_AT = 0.70, 0.40


@dataclass
class ScoredPosition:
    ctx: PositionContext
    signals: list[SignalResult]
    contributions: dict[str, float]
    score: float
    explanation: str
    hedge_ratio: float = 0.0
    extra: dict = field(default_factory=dict)


def short_account(account_id: str) -> str:
    """Public-facing display form of a pseudonymous wallet: never show it in full."""
    return f"{account_id[:6]}…{account_id[-4:]}" if len(account_id) > 12 else account_id


def score_position(ctx: PositionContext) -> ScoredPosition:
    results = [fn(ctx) for fn in ALL_SIGNALS]
    contrib = {r.name: round(100 * WEIGHTS[r.name] * r.score, 2) for r in results}
    hedge_ratio = min(1.0, ctx.hedge_notional / ctx.buy_notional) if ctx.buy_notional else 0.0
    contrib["hedge_adjustment"] = round(-HEDGE_MAX_PENALTY * hedge_ratio, 2)
    contrib["exit_adjustment"] = round(-EXIT_MAX_PENALTY * ctx.exited_ratio, 2)
    total = max(0.0, round(sum(contrib.values()), 1))
    return ScoredPosition(ctx, results, contrib, total, explain(ctx, results, contrib, total), hedge_ratio)


def explain(ctx: PositionContext, results: list[SignalResult], contrib: dict, total: float) -> str:
    """One sentence a compliance officer can read aloud."""
    ev = {r.name: r.evidence for r in results}
    t, lp, fw = ev["pre_resolution_timing"], ev["low_probability_conviction"], ev["fresh_single_purpose_wallet"]
    ce = ev["coordinated_entry"]
    bits = [f"{money(ctx.buy_notional)} on '{ctx.outcome}' at avg {ctx.vwap:.2f}"]
    if t.get("share_within_72h", 0) >= 0.5:
        mkt = t.get("market_share_within_72h")
        bits.append(f"{t['share_within_72h']:.0%} of it in the 72 h before the outcome became public"
                    + (f" (market: {mkt:.0%})" if mkt is not None else ""))
    else:
        bits.append(f"built ~{_h(t.get('weighted_avg_lead_hours', 0))} before the outcome became public")
    age = fw.get("account_age_days_at_first_buy")
    if age is not None and age <= 14 and fw.get("first_seen_source") == "venue_join_date":
        bits.append(f"from an account {age:.0f} days old")
    elif fw.get("markets_traded", 99) <= 3:
        bits.append(f"from an account that has traded only {fw['markets_traded']} market(s)")
    if ce.get("winner_side_low_history_peers", 0) >= 2 and contrib.get("coordinated_entry", 0) > 0:
        bits.append(f"alongside {ce['winner_side_low_history_peers']} other low-history buyers of the same long shot")
    if ctx.exited_ratio >= 0.5:
        bits.append(f"but {ctx.exited_ratio:.0%} was sold before the outcome became public")
    top = max((k for k in contrib if k not in ADJUSTMENTS), key=lambda k: contrib[k])
    return (f"Score {total:.0f}: " + ", ".join(bits) +
            f" — largest driver: {LABELS[top].lower()} (+{contrib[top]:.0f}); paid out {money(lp['position_payout'])}.")


def signal_strength(scores: dict[str, float]) -> dict[str, list[str]]:
    """Group signal names by strength (0..1 scores) — used by the case-file rating rubric."""
    return {
        "strong": [k for k, v in scores.items() if v >= STRONG_AT],
        "supporting": [k for k, v in scores.items() if SUPPORTING_AT <= v < STRONG_AT],
    }


def risk_band(total: float, scores: dict[str, float], exited_ratio: float,
              news_before_first_buy: bool = False) -> tuple[str, str]:
    """Deterministic starting point for the case-file risk rating, with the rule that fired.

    The case writer may depart from it, but must say why.
    """
    band, rule = _base_band(total, scores, exited_ratio)
    if news_before_first_buy and band != "Low":
        lower = {"High": "Medium", "Medium": "Low"}[band]
        return lower, f"{rule}; moved down from {band} because relevant news was published before the first buy"
    return band, rule


def _base_band(total: float, scores: dict[str, float], exited_ratio: float) -> tuple[str, str]:
    st = signal_strength(scores)
    strong_beh = [k for k in st["strong"] if k in BEHAVIOURAL]
    any_beh = [k for k in st["strong"] + st["supporting"] if k in BEHAVIOURAL]
    if exited_ratio >= 0.5:
        return "Low", "most of the position was sold before the outcome became public"
    if not any_beh:
        return "Low", ("no behavioural signal (timing, low-probability conviction, coordinated entry, price leadership, "
                       "track record) is at least supporting")
    if total < ALERT_THRESHOLD:
        return "Low", f"engine score below the review threshold of {ALERT_THRESHOLD:.0f}"
    if total >= 65 and len(st["strong"]) >= 3 and strong_beh:
        return "High", "score of 65 or more with at least three strong signals, including a behavioural one"
    return "Medium", "above the review threshold, but without three strong signals including a behavioural one"


def _h(hours: float) -> str:
    return f"{hours:.0f} h" if hours < 72 else f"{hours / 24:.0f} days"
