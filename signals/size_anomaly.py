"""Signal 4 — Size anomaly.

Question: is this position unusually large — for this market, and for this account?

* vs the market: robust z-score of log10(position $) against every pre-certainty
  position in the same market: z = (log x − median) / (1.4826 × MAD).
  Position sizes are heavy-tailed, so we work on the log scale and use median/MAD
  (not mean/std) so a few whales don't hide each other. z ≤ 1 → 0, z ≥ 4 → 1.
* vs the account's own history: ratio of this position to the median of the
  account's other positions (needs ≥3 others). 1× → 0, ≥30× → 1.
score = 0.7·market + 0.3·own   (market only, if there is no own history)
"""

from __future__ import annotations

import math
import statistics

from signals.base import PositionContext, SignalResult, clip01, money

NAME = "size_anomaly"
MIN_MAD = 0.10       # log10 units; avoids division by ~0 in uniform markets
MIN_OWN_HISTORY = 3


def size_anomaly(ctx: PositionContext) -> SignalResult:
    x = ctx.buy_notional
    lg = math.log10(max(x, 1e-9))
    if ctx.market_log_median is not None:
        mad = max(ctx.market_log_mad or 0.0, MIN_MAD)
        z = (lg - ctx.market_log_median) / (1.4826 * mad)
        s_market = clip01((z - 1) / 3)
        typical = 10 ** ctx.market_log_median
    else:
        z, s_market, typical = None, 0.0, None

    others = [v for v in ctx.other_position_notionals if v > 0]
    if len(others) >= MIN_OWN_HISTORY:
        own_median = statistics.median(others)
        ratio = x / own_median
        s_own = clip01(math.log10(max(ratio, 1e-9)) / math.log10(30))
        score = 0.7 * s_market + 0.3 * s_own
    else:
        own_median, ratio, s_own = None, None, None
        score = s_market

    pct = ctx.market_percentile
    parts = []
    if typical is not None:
        parts.append(f"The {money(x)} position is {x / typical:,.0f}× the typical position in this market "
                     f"({money(typical)}; top {max(0.1, 100 * (1 - (pct or 0))):.1f}% of {ctx.market_n_positions:,} positions)")
    if ratio is not None:
        parts.append(f"{ratio:,.1f}× this account's usual position size ({money(own_median)})")
    elif typical is not None:
        parts.append("the account has too little other history to compare against")
    reason = "; ".join(parts) + "." if parts else "No size distribution available for this market."
    evidence = {
        "position_notional": round(x, 2),
        "market_typical_position": round(typical, 2) if typical else None,
        "multiple_of_market_typical": round(x / typical, 1) if typical else None,
        "market_robust_z": round(z, 2) if z is not None else None,
        "market_percentile": round(pct, 4) if pct is not None else None,
        "market_n_positions": ctx.market_n_positions,
        "account_other_positions": len(others),
        "account_median_other_position": round(own_median, 2) if own_median else None,
        "multiple_of_account_typical": round(ratio, 1) if ratio else None,
        "part_scores": {"market": round(s_market, 3), "own": round(s_own, 3) if s_own is not None else None},
    }
    return SignalResult(NAME, score, reason, evidence)
