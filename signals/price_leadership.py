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

Move initiation: some informed buying *is* the move — the account buys the long shot
so heavily that its own buying drives the price up, before the news is public (a
follow-through measure misses that, because the price paid already includes the move).
Each market's sharpest pre-public run-up is found (see scoring/features.py); the
account's share of the buying that took the price halfway up is its initiator share:
    initiation = clip(share × rise / 0.5)     (needs ≥ $500 of that initiating buying)
e.g. 90% of the buying behind a 0.03 → 0.75 run-up scores 1.0.
The signal is the larger of follow-through and initiation.

Unlike the low-probability signal this works at any entry price (a 0.55 buy followed by
a drift to 0.85 before the news scores well), which covers its blind spot. Unlike timing,
it asks whether the market *followed* the account, not how close to the news it bought.
"""

from __future__ import annotations

from signals.base import PositionContext, SignalResult, clip01, money

NAME = "price_leadership"
FULL_AT = 0.50
INITIATION_FULL_AT = 0.50
MIN_INITIATOR_USD = 500


def initiation(ctx: PositionContext) -> float:
    if not ctx.move_rise or ctx.move_initiator_usd < MIN_INITIATOR_USD:
        return 0.0
    return clip01(ctx.move_initiator_share * ctx.move_rise / INITIATION_FULL_AT)


def price_leadership(ctx: PositionContext) -> SignalResult:
    init = initiation(ctx)
    init_txt = ""
    if init > 0:
        init_txt = (f" It also did {ctx.move_initiator_share:.0%} of the buying ({money(ctx.move_initiator_usd)}) that "
                    f"started the market's sharpest run-up before the outcome was public, a rise of "
                    f"{ctx.move_rise:.2f} in the winner's price"
                    + (f" from {ctx.move_started_at:%Y-%m-%d %H:%M} UTC" if ctx.move_started_at else "") + ".")
    init_ev = {"move_initiator_share": round(ctx.move_initiator_share, 4), "move_initiator_usd": round(ctx.move_initiator_usd, 2),
               "market_run_up": round(ctx.move_rise, 4) if ctx.move_rise else None, "initiation_score": round(init, 3)}
    legs = [b for b in ctx.buys if b.later_price is not None and b.price < 1]
    if not legs:
        return SignalResult(NAME, init, ("No later market price before the outcome became public to compare against."
                                         + init_txt).strip(), {"legs_with_later_price": 0, **init_ev})
    tot = sum(b.notional for b in legs)
    caps = [(b, (b.later_price - b.price) / (1 - b.price)) for b in legs]
    value = sum(b.notional * max(c, 0.0) for b, c in caps) / tot
    avg_paid = sum(b.notional * b.price for b in legs) / tot
    avg_later = sum(b.notional * b.later_price for b in legs) / tot
    follow = clip01(value / FULL_AT)
    score = max(follow, init)
    if value <= 0.02:
        reason = (f"The winner's price did not move up after these buys (average {avg_paid:.2f} paid, "
                  f"{avg_later:.2f} a day later or just before the outcome became public).")
    else:
        reason = (f"After the account bought at an average {avg_paid:.2f}, the winner's price rose to {avg_later:.2f} "
                  f"before the outcome became public — the market covered {value:.0%} of the remaining distance "
                  f"to certainty after these buys, on {money(tot)} of buying.")
    reason += init_txt
    return SignalResult(NAME, score, reason, {**init_ev, "follow_through_score": round(follow, 3),
        "avg_price_paid": round(avg_paid, 4), "avg_price_later": round(avg_later, 4),
        "share_of_remaining_move_after_buys": round(value, 4), "legs_with_later_price": len(legs),
        "horizon_hours": 24})
