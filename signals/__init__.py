"""Detection signals. Each is a pure function PositionContext -> SignalResult."""

from signals.category_prior import category_prior
from signals.coordinated_entry import coordinated_entry
from signals.fresh_wallet import fresh_single_purpose_wallet
from signals.low_probability import low_probability_conviction
from signals.size_anomaly import size_anomaly
from signals.timing import pre_resolution_timing

ALL_SIGNALS = [
    pre_resolution_timing,
    low_probability_conviction,
    fresh_single_purpose_wallet,
    size_anomaly,
    category_prior,
    coordinated_entry,
]
