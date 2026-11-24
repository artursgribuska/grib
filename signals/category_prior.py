"""Signal 7 — Category prior.

Question: is this the kind of market where a small group can know the answer before
the public does?

The weights reflect where non-public information realistically exists, informed by
the enforcement and surveillance cases in prediction and conventional markets:
corporate and personnel decisions are known to insiders days in advance; award
results are known to organisers and vote counters; government/military actions are
known to officials; macro data is under embargo but tightly controlled; elections
are counted publicly; sports and price markets resolve in public view. A high prior
is a reason to look closer, never evidence on its own.
"""

from __future__ import annotations

from signals.base import PositionContext, SignalResult

NAME = "category_prior"

CATEGORY_WEIGHTS = {
    "personnel": 1.00,
    "government_military": 0.95,
    "corporate": 0.90,
    "awards_entertainment": 0.85,
    "economics_data": 0.60,
    "elections": 0.45,
    "other": 0.40,
    "public_metric": 0.25,
    "sports": 0.15,
    "price_threshold": 0.10,
    "crypto_price": 0.05,
}

WHY = {
    "personnel": "appointments, resignations and hires are decided by a few people before any announcement",
    "government_military": "officials and staff know of government or military actions before they are public",
    "corporate": "company insiders know of announcements, launches and deals before the public",
    "awards_entertainment": "organisers, voters and production staff can know results before they are published",
    "economics_data": "macro releases are embargoed, but leaks are rare and tightly controlled",
    "elections": "results are counted in public, though some early or internal information can exist",
    "other": "no specific reason to expect concentrated non-public information",
    "public_metric": "the outcome is a public count or ranking that anyone can track in real time",
    "sports": "results are decided in public view (match-fixing is a separate risk not scored here)",
    "price_threshold": "the outcome is a public market price",
    "crypto_price": "the outcome is a public crypto price over a short window",
}


def category_prior(ctx: PositionContext) -> SignalResult:
    cat = ctx.category if ctx.category in CATEGORY_WEIGHTS else "other"
    w = CATEGORY_WEIGHTS[cat]
    level = "high" if w >= 0.8 else "medium" if w >= 0.4 else "low"
    reason = f"Category '{cat.replace('_', ' ')}' has a {level} prior for non-public information: {WHY[cat]}."
    return SignalResult(NAME, w, reason, {"category": cat, "category_weight": w})
