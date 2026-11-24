"""Signal 1 — Pre-resolution timing, relative to the market's own rhythm.

Question it answers: was this position built unusually late *for this market* — in the
short window before the outcome became public — while the outcome still looked unlikely?

"Bought in the last 72 h" means nothing on its own: a basketball game or a 5-minute
crypto window trades almost entirely in its final hours, while a resignation market
trades for months. So we score how much later this position's money arrived than the
market's money in general.

1. Lateness of a buy, by its lead time before public certainty (monotonic):
       ≤ 24h  1.00     24h – 72h  0.75     72h – 7 days  0.30     > 7 days  0.10
2. position_lateness = notional-weighted lateness of the position's buys;
   market_lateness   = the same over *every* pre-certainty buy in the market (baseline).
3. excess = clip((position_lateness − baseline) / EXCESS_FULL_AT, 0, 1), where
   baseline = min(market_lateness, cap for the market's age). A market where all money
   is late *by design* (sports, 5-minute crypto: open for days or hours) leaves no room
   for excess. But a market open for months that suddenly does most of its business in
   its last days is itself anomalous — often because the news is leaking — so its
   baseline is capped at the 90th percentile for markets of that age in the demo data
   (≤14 days 1.0, 45 days 0.9, ≥90 days 0.6; linear in between). A tighter cap (75th
   percentile) cost recall in the backtest by lifting ordinary late buyers too.
4. price/news factor = notional-and-lateness-weighted average of (1 − price) × d, where
   d = 0.70 for buys in the last hour (could be reacting to fast-breaking news), else 1.
   Buying a 0.95 favourite late is not informative; buying a 0.15 long shot late is.
5. score = clip(excess × price factor / FULL_SCORE_AT, 0, 1).

If no market baseline is available, excess = position_lateness (the pre-baseline behaviour).
"""

from __future__ import annotations

import numpy as np

from signals.base import PositionContext, SignalResult, clip01, hours_str, money

NAME = "pre_resolution_timing"
WINDOWS_H = (1, 24, 72)
FULL_SCORE_AT = 0.75
EXCESS_FULL_AT = 0.30      # lateness units above the market baseline that count as fully "late"
LAST_HOUR_DISCOUNT = 0.70


AGE_CAPS = ((14 * 24, 1.0), (45 * 24, 0.9), (90 * 24, 0.6))   # (market lifetime hours, baseline cap)


def baseline_cap(lifetime_h: float | None) -> float:
    """Highest lateness baseline a market of this age may claim as 'normal'."""
    if lifetime_h is None:
        return 1.0
    (h0, c0) = AGE_CAPS[0]
    if lifetime_h <= h0:
        return c0
    for (h1, c1) in AGE_CAPS[1:]:
        if lifetime_h <= h1:
            return c0 + (c1 - c0) * (lifetime_h - h0) / (h1 - h0)
        h0, c0 = h1, c1
    return AGE_CAPS[-1][1]


def lateness(lead_h: float) -> float:
    if lead_h < 0:
        return 0.0
    if lead_h <= 24:
        return 1.00
    if lead_h <= 72:
        return 0.75
    if lead_h <= 24 * 7:
        return 0.30
    return 0.10


def lateness_array(lead_h: np.ndarray) -> np.ndarray:
    """Vectorised `lateness` (used to build the market baselines)."""
    return np.select([lead_h < 0, lead_h <= 24, lead_h <= 72, lead_h <= 24 * 7], [0.0, 1.0, 0.75, 0.30], 0.10)


def lead_weight(lead_h: float) -> float:
    """Per-buy weight used in the price/news factor: lateness with the last-hour discount."""
    return lateness(lead_h) * (LAST_HOUR_DISCOUNT if 0 <= lead_h <= 1 else 1.0)


def pre_resolution_timing(ctx: PositionContext) -> SignalResult:
    total = ctx.buy_notional
    if total <= 0:
        return SignalResult(NAME, 0.0, "No pre-certainty buys.", {})
    late_sum = weighted = 0.0
    in_window = {w: 0.0 for w in WINDOWS_H}
    for b in ctx.buys:
        lead_h = (ctx.certainty_at - b.ts).total_seconds() / 3600
        late = lateness(lead_h)
        late_sum += b.notional * late
        weighted += b.notional * lead_weight(lead_h) * (1 - b.price)
        for w in WINDOWS_H:
            if 0 <= lead_h <= w:
                in_window[w] += b.notional
    pos_late = late_sum / total
    price_factor = weighted / late_sum if late_sum else 0.0
    raw_base = ctx.market_avg_lateness
    base = min(raw_base, baseline_cap(ctx.market_lifetime_hours)) if raw_base is not None else None
    excess = clip01((pos_late - base) / EXCESS_FULL_AT) if base is not None else pos_late
    raw = excess * price_factor
    score = clip01(raw / FULL_SCORE_AT)

    share = {w: in_window[w] / total for w in WINDOWS_H}
    w72 = in_window[72]
    vwap_72 = _vwap([b for b in ctx.buys if 0 <= (ctx.certainty_at - b.ts).total_seconds() / 3600 <= 72])
    lead_wavg = sum(b.notional * (ctx.certainty_at - b.ts).total_seconds() / 3600 for b in ctx.buys) / total
    mkt72 = ctx.market_share_within_72h
    vs = f" (vs {mkt72:.0%} of all buying in this market)" if mkt72 is not None else ""
    if w72 > 0:
        reason = (f"{share[72]:.0%} of the {money(total)} position was bought in the 72 h before the outcome "
                  f"became public{vs} ({share[24]:.0%} within 24 h, {share[1]:.0%} within 1 h), "
                  f"at an average price of {vwap_72:.2f}.")
    else:
        reason = (f"The {money(total)} position was built on average {hours_str(lead_wavg)} before the "
                  f"outcome became public — outside the 72 h window.")
    if base is not None and raw_base is not None and base < raw_base - 0.05:
        reason += (f" This market had been open for {ctx.market_lifetime_hours / 24:.0f} days yet did most of its "
                   f"business at the end, which is unusual for a market of that age, so that late surge is not "
                   f"treated as normal.")
    if base is not None:
        if excess == 0:
            reason += " That is no later than the market's money in general, so timing adds nothing here."
        elif excess < 1:
            reason += " That is somewhat later than the market's money in general."
        else:
            reason += " That is much later than the market's money in general."
    evidence = {
        "position_notional": round(total, 2),
        "notional_within_1h": round(in_window[1], 2),
        "notional_within_24h": round(in_window[24], 2),
        "notional_within_72h": round(w72, 2),
        "share_within_1h": round(share[1], 4),
        "share_within_24h": round(share[24], 4),
        "share_within_72h": round(share[72], 4),
        "market_share_within_72h": round(mkt72, 4) if mkt72 is not None else None,
        "market_lifetime_hours": round(ctx.market_lifetime_hours, 1) if ctx.market_lifetime_hours is not None else None,
        "position_lateness": round(pos_late, 4),
        "market_lateness_baseline": round(base, 4) if base is not None else None,
        "market_lateness_observed": round(raw_base, 4) if raw_base is not None else None,
        "excess_lateness_score": round(excess, 4),
        "avg_price_within_72h": round(vwap_72, 4) if w72 else None,
        "weighted_avg_lead_hours": round(lead_wavg, 2),
        "certainty_at": ctx.certainty_at.isoformat(sep=" ", timespec="minutes"),
        "raw_timing_value": round(raw, 4),
    }
    return SignalResult(NAME, score, reason, evidence)


def _vwap(buys) -> float:
    size = sum(b.size for b in buys)
    return sum(b.notional for b in buys) / size if size else 0.0
