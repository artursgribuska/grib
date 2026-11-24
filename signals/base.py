"""Shared types for detection signals.

Every signal is a pure function: PositionContext -> SignalResult. No database, no
network, no venue-specific fields — so each is easy to unit-test and to explain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Buy:
    ts: datetime
    price: float     # implied probability paid (0..1)
    size: float      # shares / units
    notional: float  # price * size
    later_price: float | None = None   # winner's market price ~24 h later, capped just before public certainty
    before_price: float | None = None  # winner's market price just before this buy


@dataclass
class PositionContext:
    """Everything the signals may know about one account's position in one market."""
    market_id: str
    account_id: str
    outcome: str                      # the outcome bought (the eventual winner)
    question: str
    category: str
    certainty_at: datetime            # when the outcome became publicly (near-)certain
    resolved_at: datetime
    market_volume: float              # matched volume of the market (currency units)
    buys: list[Buy]                   # pre-certainty buys of `outcome`
    # account facts
    account_first_seen_at: datetime | None = None
    account_first_seen_source: str = "unknown"   # 'venue_join_date' | 'dataset_first_trade'
    lifetime_markets: int | None = None          # from the venue, if known
    dataset_markets: int = 1                     # markets this account traded in our data
    dataset_buy_notional: float = 0.0            # account's total buys in our data
    other_position_notionals: list[float] = field(default_factory=list)  # account's positions elsewhere
    # market size distribution (log10 of position notionals)
    market_log_median: float | None = None
    market_log_mad: float | None = None
    market_percentile: float | None = None       # 0..1 rank of this position in its market
    market_n_positions: int = 0
    hedge_notional: float = 0.0                  # same account's buys of the losing outcome
    sold_size_pre: float = 0.0                   # shares of `outcome` sold back before certainty
    sold_notional_pre: float = 0.0               # proceeds of those sales
    # market timing baseline (how late this market's money arrives in general)
    market_avg_lateness: float | None = None
    market_share_within_72h: float | None = None
    market_lifetime_hours: float | None = None
    # entry cohort: other low-history long-shot buyers entering within the cohort window
    cohort_winner_peers: int = 0
    cohort_loser_peers: int = 0
    cohort_winner_peer_usd: float = 0.0
    cohort_funding_links: int = 0                # cohort peers sharing a personal funder with this wallet
    funding_scan_status: str | None = None       # None = never scanned; 'ok'; 'archive_unavailable'; ...
    # long-shot bets by this account in OTHER markets of the dataset
    other_long_shots: int = 0
    other_long_shot_wins: int = 0
    other_long_shot_expected_wins: float = 0.0   # Σ price paid = wins the market expected
    other_long_shot_variance: float = 0.0        # Σ p(1 − p)

    @property
    def buy_notional(self) -> float:
        return sum(b.notional for b in self.buys)

    @property
    def buy_size(self) -> float:
        return sum(b.size for b in self.buys)

    @property
    def vwap(self) -> float:
        s = self.buy_size
        return self.buy_notional / s if s else 0.0

    @property
    def held_size(self) -> float:
        """Shares still held when the outcome became public."""
        return max(0.0, self.buy_size - self.sold_size_pre)

    @property
    def exited_ratio(self) -> float:
        """Share of the bought shares sold back before the outcome became public (0..1)."""
        return clip01(self.sold_size_pre / self.buy_size) if self.buy_size else 0.0

    @property
    def first_buy_ts(self) -> datetime:
        return min(b.ts for b in self.buys)


@dataclass
class SignalResult:
    name: str
    score: float          # 0..1
    reason: str           # one human-readable sentence
    evidence: dict        # the numbers behind the reason (all appear in the case file bundle)

    def __post_init__(self):
        self.score = float(max(0.0, min(1.0, self.score)))


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def money(x: float) -> str:
    return f"${x:,.0f}"


def hours_str(h: float) -> str:
    if h < 1:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"
