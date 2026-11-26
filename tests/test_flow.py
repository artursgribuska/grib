"""Account-free watch: fires on a one-sided long-shot surge, stays quiet on ordinary drift; no lookahead."""
from datetime import datetime, timedelta

import pandas as pd

from watch import flow

T0 = datetime(2026, 10, 1)


def _market(surge: bool) -> pd.DataFrame:
    rows = []
    for h in range(24 * 6):                     # six quiet days: small two-sided trading around 0.15
        ts = T0 + timedelta(hours=h)
        rows += [{"ts": ts, "side": "buy", "outcome": "Yes", "price": 0.15, "notional": 60.0},
                 {"ts": ts + timedelta(minutes=30), "side": "buy", "outcome": "No", "price": 0.85, "notional": 60.0}]
    end = T0 + timedelta(days=6)
    for i in range(12):                         # then six hours
        ts = end + timedelta(minutes=30 * i)
        if surge:                               # heavy one-sided buying lifting Yes from 0.15 to 0.50
            rows.append({"ts": ts, "side": "buy", "outcome": "Yes", "price": 0.15 + 0.03 * i, "notional": 2500.0})
        else:                                   # the same rise on thin, two-sided trading
            rows += [{"ts": ts, "side": "buy", "outcome": "Yes", "price": 0.15 + 0.03 * i, "notional": 60.0},
                     {"ts": ts, "side": "buy", "outcome": "No", "price": 0.85 - 0.03 * i, "notional": 60.0}]
    t = pd.DataFrame(rows).assign(market_id="m", question="Q?", fill_rows=1.0)
    t["ts"] = pd.to_datetime(t.ts)
    return t.sort_values("ts").reset_index(drop=True)


def test_fires_on_one_sided_surge_only():
    hit = flow.scan(_market(True))
    assert len(hit) == 1 and hit[0].outcome == "Yes" and hit[0].price_before <= flow.MAX_START
    assert hit[0].one_sided >= flow.MIN_ONE_SIDED and hit[0].window_buy_usd >= flow.MIN_USD
    assert flow.scan(_market(False)) == []


def test_no_lookahead():
    t = _market(True)
    at = flow.scan(t)[0].at
    hour = at - timedelta(hours=1)                               # the hour whose trades triggered the alert
    assert all(a.at < at for a in flow.scan(t[t.ts < hour]))     # without them, nothing fires at that time
    assert all(a.at <= at for a in flow.scan(t, until=hour))     # `until` never lets later trades in
    assert all(a.at > at for a in flow.scan(t, since=at))        # `since` only returns later firings
