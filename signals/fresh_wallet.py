"""Signal 3 — Fresh / single-purpose wallet.

Question: does this look like an account created (or used) just for this bet?

Three parts, each 0..1:
  * age        exp(−days/7) where days = account age when it first bought into this
               market (0 days → 1.0, 1 week → 0.37, 1 month → 0.01). If the venue's
               join date is unknown we only know the account's first trade in our
               sample, which can make an old account look new — so that part is
               down-weighted by half.
  * few markets  1 − log10(n)/2 for n markets ever traded (1 → 1.0, 10 → 0.5, 100 → 0)
  * concentration  share of the account's buys (in our data) that went into this market
score = 0.45·age + 0.30·few markets + 0.25·concentration
"""

from __future__ import annotations

import math

from signals.base import PositionContext, SignalResult, clip01

NAME = "fresh_single_purpose_wallet"
AGE_HALF_LIFE_DAYS = 7
WEIGHTS = {"age": 0.45, "few_markets": 0.30, "concentration": 0.25}


def fresh_single_purpose_wallet(ctx: PositionContext) -> SignalResult:
    known_join = ctx.account_first_seen_source == "venue_join_date"
    if ctx.account_first_seen_at is not None:
        age_days = max(0.0, (ctx.first_buy_ts - ctx.account_first_seen_at).total_seconds() / 86400)
        s_age = math.exp(-age_days / AGE_HALF_LIFE_DAYS)
        if not known_join:
            s_age *= 0.5
    else:
        age_days, s_age = None, 0.0

    n_markets = ctx.lifetime_markets if ctx.lifetime_markets else ctx.dataset_markets
    n_markets = max(1, int(n_markets or 1))
    s_few = clip01(1 - math.log10(n_markets) / 2)

    conc = ctx.buy_notional / ctx.dataset_buy_notional if ctx.dataset_buy_notional else 1.0
    s_conc = clip01(conc)

    score = WEIGHTS["age"] * s_age + WEIGHTS["few_markets"] * s_few + WEIGHTS["concentration"] * s_conc

    if age_days is None:
        age_txt = "account age unknown"
    elif known_join:
        age_txt = f"account was {_days(age_days)} old (venue join date) at its first buy here"
    else:
        age_txt = f"first trade in our data was {_days(age_days)} before this position (join date unknown)"
    src = "on the venue overall" if ctx.lifetime_markets else "in our sample"
    reason = (f"{age_txt[0].upper() + age_txt[1:]}; it has traded {n_markets} market"
              f"{'s' if n_markets != 1 else ''} {src}, and {conc:.0%} of its buying in our data went into this market.")
    evidence = {
        "account_age_days_at_first_buy": round(age_days, 2) if age_days is not None else None,
        "first_seen_source": ctx.account_first_seen_source,
        "first_seen_at": ctx.account_first_seen_at.isoformat(sep=" ", timespec="minutes") if ctx.account_first_seen_at else None,
        "markets_traded": n_markets,
        "markets_traded_source": "venue_lifetime" if ctx.lifetime_markets else "dataset",
        "share_of_account_buying_in_this_market": round(conc, 4),
        "account_dataset_buy_notional": round(ctx.dataset_buy_notional, 2),
        "part_scores": {"age": round(s_age, 3), "few_markets": round(s_few, 3), "concentration": round(s_conc, 3)},
    }
    return SignalResult(NAME, score, reason, evidence)


def _days(d: float) -> str:
    return f"{d * 24:.0f} hours" if d < 2 else f"{d:.0f} days"
