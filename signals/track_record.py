"""Signal 6 — Long-shot track record.

Question: has this account won more long shots elsewhere than the prices it paid imply?

Market prices are probabilities, so if an account bought long shots (average price below
0.35, at least $250) in other markets of the dataset, the number it should have won is
simply the sum of the prices it paid. Winning many more than that is a calibrated
surprise:
    z     = (wins − Σp) / √Σp(1 − p)
    score = clip((z − 1) / 2, 0, 1), and 0 unless it won at least 2 other long shots

e.g. 3 wins from 5 bets at 0.20 (1 expected): z ≈ 2.2 → 0.62. One lucky win scores 0.
Covers only the markets in the dataset, so most accounts have no record (score 0).
"""

from __future__ import annotations

import math

from signals.base import PositionContext, SignalResult, clip01

NAME = "long_shot_track_record"
MIN_WINS = 2


def long_shot_track_record(ctx: PositionContext) -> SignalResult:
    n, w = ctx.other_long_shots, ctx.other_long_shot_wins
    exp, var = ctx.other_long_shot_expected_wins, ctx.other_long_shot_variance
    z = (w - exp) / math.sqrt(var) if var > 0 else 0.0
    score = clip01((z - 1) / 2) if w >= MIN_WINS else 0.0
    if n == 0:
        reason = "No long-shot bets by this account in other markets of the dataset."
    else:
        reason = (f"In {n} other market{'s' if n != 1 else ''} this account bought a long shot (below 0.35); "
                  f"{w} won, against about {exp:.1f} that the prices paid implied.")
        reason += (" That is more than luck usually explains." if score > 0
                   else " That is within what luck explains.")
    return SignalResult(NAME, score, reason, {
        "other_long_shots": n, "other_long_shot_wins": w,
        "expected_wins_from_prices": round(exp, 2), "excess_z": round(z, 2)})
