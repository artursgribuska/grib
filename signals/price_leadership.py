"""Signal 5 — Price leadership.

Question: did the market move toward the eventual winner *after* this account bought,
while the outcome was still not public? Someone trading on information gets ahead of
the price; someone chasing news or momentum buys after the move.

For each buy we compare the price paid (p0) with the winner's market price 24 h later,
capped 5 minutes before the estimated public time (p1):
    captured = (p1 − p0) / (1 − p0)    share of the remaining distance to 1.0 the
                                        market covered after the buy, before it was public
Chasing a move shows up as buying at an already-high price, which leaves little room.
Position value = notional-weighted mean of max(captured, 0); score = clip(value / 0.5).

Unlike the low-probability signal this works at any entry price (a 0.55 buy followed by
a drift to 0.85 before the news scores well), which covers its blind spot. Unlike timing,
it asks whether the market *followed* the account, not how close to the news it bought.
"""

from __future__ import annotations

from signals.base import PositionContext, SignalResult, clip01, money

NAME = "price_leadership"
FULL_AT = 0.50


def price_leadership(ctx: PositionContext) -> SignalResult:
    legs = [b for b in ctx.buys if b.later_price is not None and b.price < 1]
    if not legs:
        return SignalResult(NAME, 0.0, "No later market price before the outcome became public to compare against.",
                            {"legs_with_later_price": 0})
    tot = sum(b.notional for b in legs)
    caps = [(b, (b.later_price - b.price) / (1 - b.price)) for b in legs]
    value = sum(b.notional * max(c, 0.0) for b, c in caps) / tot
    avg_paid = sum(b.notional * b.price for b in legs) / tot
    avg_later = sum(b.notional * b.later_price for b in legs) / tot
    score = clip01(value / FULL_AT)
    if value <= 0.02:
        reason = (f"The winner's price did not move up after these buys (average {avg_paid:.2f} paid, "
                  f"{avg_later:.2f} a day later or just before the outcome became public).")
    else:
        reason = (f"After the account bought at an average {avg_paid:.2f}, the winner's price rose to {avg_later:.2f} "
                  f"before the outcome became public — the market covered {value:.0%} of the remaining distance "
                  f"to certainty after these buys, on {money(tot)} of buying.")
    return SignalResult(NAME, score, reason, {
        "avg_price_paid": round(avg_paid, 4), "avg_price_later": round(avg_later, 4),
        "share_of_remaining_move_after_buys": round(value, 4), "legs_with_later_price": len(legs),
        "horizon_hours": 24})
