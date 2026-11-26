"""Account-free live watch: alerts from anonymous trades, for venues that publish no accounts.

The live watch (watch/live.py) needs account ids. Kalshi's public feed, and some operators'
data, have none. This watch looks at the market instead, and fires a `flow_surge` alert when,
over the last WINDOW_H hours, an outcome that was a long shot:

  - rose by at least MIN_RISE from a price of at most MAX_START,
  - took at least MIN_ONE_SIDED of all buying in the market,
  - drew at least MIN_USD of buying,
  - while the market traded at least MIN_SURGE times its usual rate (prior 7 days).

All windows look backward: an alert at hour h uses only trades up to the end of hour h, so the
same code runs live and as a replay of history. The thresholds were fixed before the first
evaluation (eval/flow_replay.py) and are reported with its results, good or bad.

    uv run python -m watch.flow --db data/kalshi_live.duckdb     # scan a database of open markets
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

import config

WINDOW_H = 6
MAX_START = 0.35
MIN_RISE = 0.15
MIN_ONE_SIDED = 0.70
MIN_USD = 5_000
MIN_SURGE = 5.0
BASE_H = 24 * 7
MIN_BASE_H = 24          # need at least a day of history to know the market's usual rate


@dataclass
class FlowAlert:
    market_id: str
    question: str
    outcome: str
    at: datetime            # end of the hour in which the alert could first fire
    price_before: float
    price_at: float
    window_buy_usd: float
    one_sided: float
    surge: float


def load(con, market_ids: list[str] | None = None) -> pd.DataFrame:
    where = "WHERE t.market_id IN (SELECT unnest(?))" if market_ids else ""
    return con.execute(f"""
        SELECT t.market_id, m.question, t.ts, t.side, t.outcome, t.price, t.notional,
               coalesce(TRY_CAST(json_extract(m.metadata_json, '$.trade_rows_per_fill') AS DOUBLE), 2) AS fill_rows
        FROM trades t JOIN markets m USING (venue, market_id) {where} ORDER BY t.market_id, t.ts""",
                       [market_ids] if market_ids else []).df()


def hourly(t: pd.DataFrame) -> pd.DataFrame:
    """Per outcome and hour: implied price (last), buying of the outcome, all buying, volume, and the
    backward-looking window features. `t` holds one market's trades."""
    outs = list(t.outcome.unique())
    if not outs:
        return pd.DataFrame()
    t = t.assign(h=t.ts.dt.floor("h"))
    idx = pd.date_range(t.h.min(), t.h.max(), freq="h")
    buy_all = t[t.side == "buy"].groupby("h").notional.sum().reindex(idx, fill_value=0.0)
    vol = (t.notional / t.fill_rows).groupby(t.h).sum().reindex(idx, fill_value=0.0)
    vol_w = vol.rolling(WINDOW_H, min_periods=1).sum()
    base = vol.shift(WINDOW_H).rolling(BASE_H, min_periods=MIN_BASE_H).sum()
    n_base = vol.shift(WINDOW_H).rolling(BASE_H, min_periods=MIN_BASE_H).count()
    base_w = base / n_base * WINDOW_H                     # usual volume per window
    frames = []
    for o in outs:
        if len(outs) == 2:
            p = np.where(t.outcome == o, t.price, 1 - t.price)
            src = t.assign(p=p)
        else:
            src = t[t.outcome == o].assign(p=lambda d: d.price)
        if src.empty:
            continue
        price = src.groupby("h").p.last().reindex(idx).ffill()
        buy_o = t[(t.side == "buy") & (t.outcome == o)].groupby("h").notional.sum().reindex(idx, fill_value=0.0)
        buy_o_w = buy_o.rolling(WINDOW_H, min_periods=1).sum()
        all_w = buy_all.rolling(WINDOW_H, min_periods=1).sum()
        start = price.shift(WINDOW_H)
        f = pd.DataFrame({"outcome": o, "price": price, "price_before": start, "rise": price - start,
                          "buy_w": buy_o_w, "one_sided": (buy_o_w / all_w.replace(0, np.nan)).fillna(0),
                          "surge": (vol_w / base_w.replace(0, np.nan)).fillna(np.inf).where(base_w.notna())}, index=idx)
        frames.append(f)
    return pd.concat(frames).rename_axis("h").reset_index() if frames else pd.DataFrame()


def fires(h: pd.DataFrame) -> pd.DataFrame:
    if h.empty:
        return h
    m = ((h.price_before <= MAX_START) & (h.rise >= MIN_RISE) & (h.one_sided >= MIN_ONE_SIDED)
         & (h.buy_w >= MIN_USD) & (h.surge >= MIN_SURGE))
    return h[m.fillna(False)]


def scan(trades: pd.DataFrame, since: datetime | None = None, until: datetime | None = None) -> list[FlowAlert]:
    """First firing per (market, outcome) in (since, until]; uses only trades up to `until`."""
    out = []
    for mid, g in trades.groupby("market_id"):
        if until is not None:
            g = g[g.ts <= until]
        f = fires(hourly(g))
        if f.empty:
            continue
        f = f.assign(at=f.h + pd.Timedelta(hours=1))
        if until is not None:
            f = f[f["at"] <= pd.Timestamp(until) + pd.Timedelta(hours=1)]
        if since is not None:
            f = f[f["at"] > pd.Timestamp(since)]
        for o, r in f.groupby("outcome"):
            r = r.iloc[0]
            out.append(FlowAlert(mid, g.question.iloc[0], o, r["at"].to_pydatetime(), round(float(r.price_before), 3),
                                 round(float(r.price), 3), round(float(r.buy_w)), round(float(r.one_sided), 3),
                                 round(float(min(r.surge, 1e6)), 1)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--hours", type=float, default=24, help="report alerts that fired in the last N hours of data")
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db, read_only=True)
    t = load(con)
    con.close()
    if t.empty:
        print("no trades")
        return 0
    end = t.ts.max()
    for x in scan(t, since=end - pd.Timedelta(hours=a.hours), until=end):
        print(json.dumps(asdict(x), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
