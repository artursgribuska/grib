"""Signal 1 — Pre-resolution timing.

Question it answers: was this position built in the short window *before* the outcome
became public, while the outcome still looked unlikely?

For each buy we take its lead time (hours before public certainty) and weight it:
    1h – 24h before certainty   1.00  (classic "knew before the announcement")
    24h – 72h                   0.75
    under 1h                    0.70  (could be reacting to fast-breaking news)
    72h – 7 days                0.30
    over 7 days                 0.10  (long-held conviction, not timing)
and multiply by (1 − price): buying a 0.95 favourite an hour early is not informative,
buying a 0.15 long shot an hour early is. The notional-weighted average is rescaled so
that ~0.75 (e.g. everything bought at ≤0.25 within the 1–24h window) scores 1.0.
"""

from __future__ import annotations

from signals.base import PositionContext, SignalResult, clip01, hours_str, money

NAME = "pre_resolution_timing"
WINDOWS_H = (1, 24, 72)
FULL_SCORE_AT = 0.75


def lead_weight(lead_h: float) -> float:
    if lead_h < 0:
        return 0.0
    if lead_h <= 1:
        return 0.70
    if lead_h <= 24:
        return 1.00
    if lead_h <= 72:
        return 0.75
    if lead_h <= 24 * 7:
        return 0.30
    return 0.10


def pre_resolution_timing(ctx: PositionContext) -> SignalResult:
    total = ctx.buy_notional
    if total <= 0:
        return SignalResult(NAME, 0.0, "No pre-certainty buys.", {})
    raw = 0.0
    in_window = {w: 0.0 for w in WINDOWS_H}
    for b in ctx.buys:
        lead_h = (ctx.certainty_at - b.ts).total_seconds() / 3600
        raw += b.notional * lead_weight(lead_h) * (1 - b.price)
        for w in WINDOWS_H:
            if 0 <= lead_h <= w:
                in_window[w] += b.notional
    raw /= total
    score = clip01(raw / FULL_SCORE_AT)

    share = {w: in_window[w] / total for w in WINDOWS_H}
    w72 = in_window[72]
    vwap_72 = _vwap([b for b in ctx.buys if 0 <= (ctx.certainty_at - b.ts).total_seconds() / 3600 <= 72])
    lead_wavg = sum(b.notional * (ctx.certainty_at - b.ts).total_seconds() / 3600 for b in ctx.buys) / total
    if w72 > 0:
        reason = (f"{share[72]:.0%} of the {money(total)} position was bought in the 72 h before the outcome "
                  f"became public ({share[24]:.0%} within 24 h, {share[1]:.0%} within 1 h), "
                  f"at an average price of {vwap_72:.2f}.")
    else:
        reason = (f"The {money(total)} position was built on average {hours_str(lead_wavg)} before the "
                  f"outcome became public — outside the 72 h window.")
    evidence = {
        "position_notional": round(total, 2),
        "notional_within_1h": round(in_window[1], 2),
        "notional_within_24h": round(in_window[24], 2),
        "notional_within_72h": round(w72, 2),
        "share_within_1h": round(share[1], 4),
        "share_within_24h": round(share[24], 4),
        "share_within_72h": round(share[72], 4),
        "avg_price_within_72h": round(vwap_72, 4) if w72 else None,
        "weighted_avg_lead_hours": round(lead_wavg, 2),
        "certainty_at": ctx.certainty_at.isoformat(sep=" ", timespec="minutes"),
        "raw_timing_value": round(raw, 4),
    }
    return SignalResult(NAME, score, reason, evidence)


def _vwap(buys) -> float:
    size = sum(b.size for b in buys)
    return sum(b.notional for b in buys) / size if size else 0.0
