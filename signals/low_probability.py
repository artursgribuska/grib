"""Signal 2 — Low-probability conviction.

Question: did the account put meaningful money on the winner while the market thought
it was unlikely (price < 0.30)?

"Surprise" of a buy = log(1/price) — a 0.10 buy carries 2.3 units of surprise, a 0.50
buy only 0.7. We weight each low-price buy's surprise by its share of the market's volume *before the
outcome became public* (what the bettor was betting against; a post-news frenzy can be
many times larger and is irrelevant), so a $20k long-shot bet in a $200k market counts
far more than in a $20M market:
    raw   = Σ_{price<0.30} log(1/price) × notional / market_volume
    score = 1 − exp(−raw / 0.03)      (raw 0.03 ≈ 0.63, raw 0.09 ≈ 0.95)
"""

from __future__ import annotations

import math

from signals.base import PositionContext, SignalResult, money

NAME = "low_probability_conviction"
LOW_PRICE = 0.30
SCALE = 0.03


def low_probability_conviction(ctx: PositionContext) -> SignalResult:
    low = [b for b in ctx.buys if 0 < b.price < LOW_PRICE]
    low_notional = sum(b.notional for b in low)
    low_size = sum(b.size for b in low)
    vol = max(ctx.market_volume, 1.0)
    raw = sum(math.log(1 / b.price) * b.notional for b in low) / vol
    score = 1 - math.exp(-raw / SCALE)
    total_cost = ctx.buy_notional
    payout = ctx.held_size  # each winning share still held pays 1 unit at resolution
    profit = payout + ctx.sold_notional_pre - total_cost
    if low:
        vwap_low = low_notional / low_size
        reason = (f"Bought {money(low_notional)} of the winning outcome at an average price of "
                  f"{vwap_low:.2f} (≈{vwap_low:.0%} implied chance) — {low_notional / vol:.1%} of the market's "
                  f"volume before the outcome became public; the position paid out {money(payout)} for a {money(profit)} profit"
                  + (f" ({ctx.exited_ratio:.0%} of the shares were sold before the outcome became public)."
                     if ctx.exited_ratio > 0 else "."))
    else:
        vwap_low = None
        reason = (f"No buys below {LOW_PRICE:.2f}; average entry price was {ctx.vwap:.2f}, "
                  f"so the bet was not against the market's view.")
    evidence = {
        "low_price_threshold": LOW_PRICE,
        "low_price_notional": round(low_notional, 2),
        "low_price_avg_price": round(vwap_low, 4) if vwap_low else None,
        "low_price_share_of_market_volume": round(low_notional / vol, 4),
        "market_volume": round(ctx.market_volume, 2),
        "surprise_weighted_share": round(raw, 4),
        "position_avg_price": round(ctx.vwap, 4),
        "position_cost": round(total_cost, 2),
        "position_payout": round(payout, 2),
        "position_profit": round(profit, 2),
        "shares_sold_before_public": round(ctx.sold_size_pre, 2),
        "share_of_position_sold_before_public": round(ctx.exited_ratio, 4),
    }
    return SignalResult(NAME, score, reason, evidence)
