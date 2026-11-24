"""Evidence bundle: every fact the case-file writer is allowed to use, as JSON.

The LLM never sees the database — only this bundle. That is what makes the
"every number in the case file must appear in the bundle" check meaningful.

Reads only canonical tables + the derived feature/score tables, so it works for any venue.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import duckdb

from scoring import features
from scoring.score import ALERT_THRESHOLD, LABELS, WEIGHTS, HEDGE_MAX_PENALTY, short_account
from signals import low_probability, timing

DISCLAIMER = ("Demo built on public data. This is anomalous activity warranting review, based on statistical "
              "signals. It is not a legal finding and does not identify or accuse any person.")

DATA_LIMITATIONS = [
    "No news or announcement feed is included: we cannot see whether public information appeared before the trades.",
    "The time the outcome became public is estimated from market prices (the winner never again traded below "
    f"{features.CERTAINTY_PRICE}), not from a verified announcement time.",
    "The wallet is pseudonymous: there is no KYC, identity, or funding-source information in this bundle.",
    "Account history covers the sample of markets loaded for this demo, plus the venue's public profile statistics.",
    "Only trades executed on this venue are visible; positions held elsewhere are unknown.",
]

MAX_TRADES = 40
MAX_PRICE_POINTS = 160


def _num(x, nd=2):
    if x is None:
        return None
    try:
        if isinstance(x, float) and math.isnan(x):
            return None
    except TypeError:
        pass
    if isinstance(x, (int,)) and not isinstance(x, bool):
        return int(x)
    return round(float(x), nd)


def _ts(x) -> str | None:
    if x is None:
        return None
    if hasattr(x, "to_pydatetime"):
        x = x.to_pydatetime()
    return x.strftime("%Y-%m-%d %H:%M UTC")


def build_bundle(con: duckdb.DuckDBPyConnection, position_id: str) -> dict:
    p = con.execute("SELECT * FROM position_scores WHERE position_id = ?", [position_id]).df()
    if p.empty:
        raise KeyError(f"unknown position {position_id}")
    p = p.iloc[0]
    mid, acct = p.market_id, p.account_id
    venue = con.execute("SELECT venue FROM markets WHERE market_id = ?", [mid]).fetchone()[0]
    m = con.execute("""SELECT s.*, mk.opened_at, d.n_positions FROM market_state s
                       JOIN markets mk USING (venue, market_id)
                       LEFT JOIN market_size_dist d USING (venue, market_id)
                       WHERE s.venue = ? AND s.market_id = ?""", [venue, mid]).df().iloc[0]
    a = con.execute("SELECT first_seen_at, metadata_json FROM accounts WHERE venue = ? AND account_id = ?",
                    [venue, acct]).fetchone()
    meta = json.loads(a[1]) if a and a[1] else {}
    hist = con.execute("SELECT dataset_markets, dataset_buy_notional FROM account_history WHERE venue=? AND account_id=?",
                       [venue, acct]).fetchone() or (None, None)
    sigs = con.execute("SELECT * FROM signal_results WHERE position_id = ?", [position_id]).df()
    contrib = json.loads(p.contributions_json)

    certainty, resolved = m.certainty_at.to_pydatetime(), m.resolved_at.to_pydatetime()
    position_cost = float(p.position_notional)
    lp_ev = json.loads(sigs.loc[sigs.signal == low_probability.NAME, "evidence_json"].iloc[0])

    bundle = {
        "case_id": position_id,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "venue": venue,
        "disclaimer": DISCLAIMER,
        "method": {
            "unit_of_review": "one account's buying of the eventual winning outcome in one market, before the outcome became public",
            "public_certainty_rule": f"first trade after which the winning outcome never again traded below {features.CERTAINTY_PRICE}",
            "minimum_position_usd": features.MIN_POSITION_USD,
            "timing_windows_hours": list(timing.WINDOWS_H),
            "low_price_threshold": low_probability.LOW_PRICE,
            "max_points_per_signal": {LABELS[k]: round(100 * w) for k, w in WEIGHTS.items()},
            "max_hedge_deduction": HEDGE_MAX_PENALTY,
            "review_threshold_score": ALERT_THRESHOLD,
        },
        "market": {
            "question": m.question,
            "category": m.category,
            "winning_outcome": m.winning_outcome,
            "opened_at": _ts(m.opened_at),
            "outcome_became_public_at": _ts(certainty),
            "officially_resolved_at": _ts(resolved),
            "hours_between_public_and_resolution": _num((resolved - certainty).total_seconds() / 3600, 1),
            "market_volume_usd": _num(m.market_volume, 0),
            "accounts_trading": int(m.n_accounts),
            "positions_before_public": int(m.n_positions or 0),
        },
        "account": {
            "display_id": short_account(acct),
            "first_seen_at": _ts(a[0]) if a else None,
            "first_seen_source": meta.get("first_seen_source", "dataset_first_trade"),
            "lifetime_markets_traded_on_venue": meta.get("lifetime_markets_traded"),
            "lifetime_biggest_win_usd": _num(meta.get("lifetime_biggest_win_usd"), 0),
            "markets_in_demo_sample": hist[0],
            "total_buying_in_demo_sample_usd": _num(hist[1], 0),
            "other_flagged_positions": _other_alerts(con, acct, position_id),
        },
        "position": {
            "outcome_bought": p.outcome,
            "cost_usd": _num(position_cost, 0),
            "average_price": _num(p.avg_price, 2),
            "payout_at_resolution_usd": _num(lp_ev["position_payout"], 0),
            "profit_usd": _num(lp_ev["position_profit"], 0),
            "first_buy_at": _ts(p.first_buy_ts),
            "hours_first_buy_before_public": _num((certainty - p.first_buy_ts.to_pydatetime()).total_seconds() / 3600, 1),
            "bought_losing_side_usd": _num(position_cost * float(p.hedge_ratio), 0),
        },
        "score": {
            "total": _num(p.score, 1),
            "points_by_signal": {LABELS[k]: _num(v, 1) for k, v in contrib.items()},
            "one_line_explanation": p.explanation,
        },
        "signals": [
            {"signal": LABELS[r.signal], "score_0_to_1": _num(r.score, 2), "points": _num(r.contribution, 1),
             "reason": r.reason, "evidence": json.loads(r.evidence_json)}
            for r in sigs.itertuples()
        ],
        "account_trades_in_market": account_trades(con, venue, mid, acct, certainty),
        "market_price_path": price_path(con, venue, mid, p.first_buy_ts.to_pydatetime(), resolved),
        "market_context": market_context(con, venue, mid, acct, certainty),
        "data_limitations": DATA_LIMITATIONS,
    }
    return bundle


def _other_alerts(con, acct: str, position_id: str) -> list[dict]:
    rows = con.execute("""SELECT question, score, position_notional FROM position_scores
                          WHERE account_id = ? AND position_id <> ? AND score >= ?
                          ORDER BY score DESC LIMIT 5""", [acct, position_id, ALERT_THRESHOLD - 10]).fetchall()
    return [{"market": q, "score": _num(s, 1), "cost_usd": _num(n, 0)} for q, s, n in rows]


def account_trades(con, venue, mid, acct, certainty: datetime) -> list[dict]:
    """The account's own trades in this market (largest first if there are too many)."""
    df = con.execute("""SELECT ts, side, outcome, price, size, notional FROM trades
                        WHERE venue = ? AND market_id = ? AND account_id = ? ORDER BY ts""",
                     [venue, mid, acct]).df()
    if len(df) > MAX_TRADES:
        df = df.nlargest(MAX_TRADES, "notional").sort_values("ts")
    return [{
        "time": _ts(r.ts), "side": r.side, "outcome": r.outcome, "price": _num(r.price, 3),
        "shares": _num(r.size, 0), "usd": _num(r.notional, 0),
        "hours_before_public": _num((certainty - r.ts.to_pydatetime()).total_seconds() / 3600, 1),
    } for r in df.itertuples()]


def price_path(con, venue, mid, first_buy: datetime, resolved: datetime) -> list[dict]:
    """Implied price of the winning outcome, sampled into buckets (last trade per bucket),
    from shortly before the account's first buy to resolution."""
    span = max((resolved - first_buy).total_seconds(), 3600)
    start_epoch = first_buy.timestamp() - 0.25 * span
    bucket = max(60, int(span * 1.25 / MAX_PRICE_POINTS))
    rows = con.execute(f"""
        SELECT to_timestamp(floor(epoch(ts) / {bucket}) * {bucket})::TIMESTAMP AS t,
               arg_max(winner_price, ts) AS price
        FROM trades_w WHERE venue = ? AND market_id = ? AND notional >= 5
          AND epoch(ts) >= ? AND ts <= ?
        GROUP BY 1 ORDER BY 1""", [venue, mid, start_epoch, resolved]).fetchall()
    return [{"time": _ts(t), "winner_price": _num(px, 3)} for t, px in rows]


def market_context(con, venue, mid, acct, certainty: datetime) -> dict:
    """How unusual was this account's pre-announcement buying relative to everyone else?"""
    r = con.execute("""
        WITH w AS (SELECT account_id, sum(notional) AS usd FROM trades_w
                   WHERE venue = ? AND market_id = ? AND side = 'buy' AND outcome = winning_outcome
                     AND ts < ? AND ts >= ? - INTERVAL 72 HOUR GROUP BY 1)
        SELECT count(*) FILTER (WHERE usd >= ? AND account_id <> ?),
               coalesce(sum(usd), 0), coalesce(sum(usd) FILTER (WHERE account_id = ?), 0)
        FROM w""", [venue, mid, certainty, certainty, features.MIN_POSITION_USD, acct, acct]).fetchone()
    others, total, mine = r
    return {
        "other_accounts_buying_winner_over_min_in_72h_before_public": int(others),
        "all_winner_buying_in_72h_before_public_usd": _num(total, 0),
        "this_account_share_of_that_buying": _num(mine / total, 3) if total else None,
    }
