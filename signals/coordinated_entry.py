"""Signal 8 — Coordinated entry.

Question: did several low-history accounts pile into the same long shot at about the
same time as this one?

One informed person can spread a bet over several new wallets; a group sharing a tip
behaves the same way. Ordinary new accounts arriving in a market split between both
sides, so the *losing* side is the control:

  winner peers  other low-history accounts (≤5 markets traded, or joined ≤14 days
                before buying) that bought ≥ $250 of the winning outcome at an average
                price ≤ 0.50, with a first buy within ±24 h of this position's first buy
  loser peers   the same, on the losing outcome(s)
  z     = (winner − loser) / √(winner + loser)        (Poisson-style excess)
  score = clip((z − 1) / 2, 0, 1), and 0 unless there are at least 2 winner peers

e.g. 4 vs 0 → 0.50, 6 vs 0 → 0.72, 10 vs 0 → 1.0, 40 vs 30 → 0.10.
Funding links (if `ingest.funding` has run): peers in the cohort that were funded by the
same personal address as this wallet, or by / to this wallet directly. One linked peer
scores at least 0.70, two or more 1.0 — shared funding is much stronger evidence than
simultaneous entry. Without funding data a high score means "look for links", not "linked".
"""

from __future__ import annotations

import math

from signals.base import PositionContext, SignalResult, clip01, money

NAME = "coordinated_entry"
MIN_WINNER_PEERS = 2
LINK_SCORES = {0: 0.0, 1: 0.70}   # ≥2 linked peers → 1.0


def coordinated_entry(ctx: PositionContext) -> SignalResult:
    w, l = ctx.cohort_winner_peers, ctx.cohort_loser_peers
    z = (w - l) / math.sqrt(w + l) if (w + l) else 0.0
    cohort_score = clip01((z - 1) / 2) if w >= MIN_WINNER_PEERS else 0.0
    links = ctx.cohort_funding_links
    link_score = LINK_SCORES.get(links, 1.0)
    score = max(cohort_score, link_score)
    if w == 0:
        reason = "No other low-history account bought this long shot within 24 h of this position."
    else:
        reason = (f"{w} other low-history account{'s' if w != 1 else ''} bought the winning outcome at a low price "
                  f"within 24 h of this position ({money(ctx.cohort_winner_peer_usd)} in total), against {l} "
                  f"that did the same on the losing side.")
        reason += (" That is more one-sided than ordinary new-account flow." if cohort_score > 0
                   else " That is not unusual for this market.")
    if links:
        reason += (f" {links} of them received USDC from the same personal address as this wallet, or from / to "
                   f"this wallet directly, around when they started trading.")
    elif ctx.funding_scan_status == "ok" and w:
        reason += " None of them shares a personal funding address with this wallet."
    evidence = {
        "winner_side_low_history_peers": w,
        "loser_side_low_history_peers": l,
        "winner_side_peer_usd": round(ctx.cohort_winner_peer_usd, 2),
        "excess_z": round(z, 2),
        "funding_linked_peers": links,
        "funding_data": ctx.funding_scan_status or "not scanned",
    }
    return SignalResult(NAME, score, reason, evidence)
