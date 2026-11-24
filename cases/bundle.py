"""Evidence bundle: every fact the case-file writer is allowed to use, as JSON.

The LLM never sees the database — only this bundle. That is what makes the
"every number in the case file must appear in the bundle" check meaningful.

Reads only canonical tables + the derived feature/score tables, so it works for any venue.
"""

from __future__ import annotations

import json
import hashlib
import math
from datetime import datetime, timezone

import duckdb

from scoring import features
from scoring.run import ROLLUP_MIN_SCORE
from scoring.score import (ALERT_THRESHOLD, BEHAVIOURAL, EXIT_MAX_PENALTY, HEDGE_MAX_PENALTY, LABELS,
                           STRONG_AT, SUPPORTING_AT, WEIGHTS, risk_band, short_account, signal_strength)
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
NO_FUNDING = ("No wallet-funding data was available for this account: accounts that entered together cannot be "
              "shown to be linked.")
FUNDING_SCOPE = ("Funding covers only USDC and pUSD (Polymarket USD) transfers into the wallet on Polygon around when it started trading; "
                 "deposits through exchanges, bridges or card on-ramps come from shared addresses and link nothing.")

RATING_RULES = [
    "Low if at least half of the position was sold before the outcome became public.",
    f"Low if no behavioural signal ({', '.join(LABELS[k] for k in BEHAVIOURAL)}) is at least supporting.",
    f"Low if the engine score is below the review threshold of {ALERT_THRESHOLD:.0f}.",
    "High if the engine score is 65 or more with at least three strong signals, including a behavioural one.",
    "Medium otherwise.",
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


def case_id(position_id: str) -> str:
    """Public-safe case reference: a short hash, so no wallet address leaks into files."""
    return "CASE-" + hashlib.sha1(position_id.encode()).hexdigest()[:10].upper()


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
    sig_scores = dict(zip(sigs.signal, sigs.score.astype(float)))
    strength = signal_strength(sig_scores)
    band, band_rule = risk_band(float(p.score), sig_scores, float(p.exited_ratio))
    funding = funding_context(con, acct)

    bundle = {
        "case_id": case_id(position_id),
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
            "max_exit_deduction": EXIT_MAX_PENALTY,
            "review_threshold_score": ALERT_THRESHOLD,
            "timing_rule": "timing is scored against how late this market's money arrives in general; "
                           "a market that trades almost entirely in its final hours gives no timing signal",
            "coordinated_entry_rule": (
                f"other low-history accounts (at most {features.LOW_HISTORY_MAX_MARKETS} markets traded, or joined at most "
                f"{features.LOW_HISTORY_MAX_AGE_DAYS} days before buying) buying at least ${features.COHORT_MIN_USD} at an average "
                f"price of at most {features.COHORT_MAX_PRICE} within {features.COHORT_WINDOW_H} h of this position, "
                "winning side compared against losing side"),
            "signal_strength_bands": {"strong": f"score_0_to_1 of at least {STRONG_AT}",
                                      "supporting": f"score_0_to_1 of at least {SUPPORTING_AT}"},
            "risk_rating_rules": RATING_RULES,
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
            "shares_sold_before_public": _num(lp_ev.get("shares_sold_before_public", 0), 0),
            "share_of_position_sold_before_public": _num(lp_ev.get("share_of_position_sold_before_public", 0), 3),
        },
        "score": {
            "total": _num(p.score, 1),
            "points_by_signal": {LABELS[k]: _num(v, 1) for k, v in contrib.items()},
            "one_line_explanation": p.explanation,
            "strong_signals": [LABELS[k] for k in strength["strong"]],
            "supporting_signals": [LABELS[k] for k in strength["supporting"]],
            "starting_risk_rating": band,
            "starting_risk_rating_rule": band_rule,
        },
        "signals": [
            {"signal": LABELS[r.signal], "score_0_to_1": _num(r.score, 2), "points": _num(r.contribution, 1),
             "reason": r.reason, "evidence": json.loads(r.evidence_json)}
            for r in sigs.itertuples()
        ],
        "account_trades_in_market": account_trades(con, venue, mid, acct, certainty),
        "market_price_path": price_path(con, venue, mid, p.first_buy_ts.to_pydatetime(), resolved),
        "market_context": market_context(con, venue, mid, acct, certainty),
        "event_context": event_context(con, mid, acct, position_id),
        "funding": funding,
        "data_limitations": DATA_LIMITATIONS + [FUNDING_SCOPE if funding["status"] == "ok" else NO_FUNDING],
    }
    return bundle


def _other_alerts(con, acct: str, position_id: str) -> list[dict]:
    rows = con.execute("""SELECT question, score, position_notional FROM position_scores
                          WHERE account_id = ? AND position_id <> ? AND score >= ?
                          ORDER BY score DESC LIMIT 5""", [acct, position_id, ALERT_THRESHOLD - 10]).fetchall()
    return [{"market": q, "score": _num(s, 1), "cost_usd": _num(n, 0)} for q, s, n in rows]


def event_context(con, mid: str, acct: str, position_id: str) -> dict:
    """Other flagged wallets on the same story (related markets grouped by event)."""
    row = con.execute("SELECT coalesce(json_extract_string(metadata_json, '$.event_title'), question) FROM markets "
                      "WHERE market_id = ?", [mid]).fetchone()
    key = row[0] if row else None
    others = con.execute(f"""
        SELECT ps.account_id, ps.question, ps.score, ps.position_notional, ps.first_buy_ts
        FROM position_scores ps JOIN markets m USING (market_id)
        WHERE coalesce(json_extract_string(m.metadata_json, '$.event_title'), m.question) = ?
          AND ps.position_id <> ? AND ps.account_id <> ? AND ps.score >= {ROLLUP_MIN_SCORE}
        ORDER BY ps.score DESC LIMIT 8""", [key, position_id, acct]).fetchall()
    return {
        "event": key,
        "other_flagged_wallets_on_this_event": len(others),
        "flagged_positions": [{"display_id": short_account(a), "market": q, "score": _num(sc, 1),
                               "cost_usd": _num(n, 0), "first_buy_at": _ts(t)} for a, q, sc, n, t in others],
        "note": f"positions by other accounts on related markets scoring at least {ROLLUP_MIN_SCORE:.0f}",
    }


def funding_context(con, acct: str) -> dict:
    """Where this wallet's first USDC came from, and which other scanned wallets share that sender."""
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "funding_senders" not in tables:
        return {"status": "not scanned"}
    scan = con.execute("SELECT status, from_ts, to_ts FROM wallet_funding_scans WHERE account_id = ?", [acct]).fetchall()
    if not scan:
        return {"status": "not scanned"}
    status, f0, f1 = scan[0]
    out = {"status": status, "window_from": _ts(f0), "window_to": _ts(f1)}
    if status != "ok":
        out["peer_pairs_sharing_a_personal_funder"] = cohort_peer_links(con, acct)
        return out
    rows = con.execute("""
        SELECT t.ts, t.amount, t.token, t.sender, coalesce(f.is_contract, s.is_contract, false),
               coalesce(f.infrastructure, false), f.wallets_funded, f.wallets
        FROM wallet_funding_transfers t LEFT JOIN funding_senders f USING (sender)
        LEFT JOIN wallet_funding_senders s USING (sender)
        WHERE t.account_id = ? ORDER BY t.amount >= ? DESC, t.ts LIMIT 10""", [acct, features.FUNDING_MIN_USD]).fetchall()
    def kind(amt, is_c, infra):
        if amt < features.FUNDING_MIN_USD:
            return "dust (ignored)"
        return "contract" if is_c else ("shared infrastructure" if infra else "personal address")
    out["incoming_transfers"] = [{
        "time": _ts(ts), "amount_usd": _num(amt, 0), "token": tok, "sender": short_account(snd),
        "sender_type": kind(amt, is_c, infra),
    } for ts, amt, tok, snd, is_c, infra, _, _ in rows]
    out["minimum_funding_usd"] = features.FUNDING_MIN_USD
    linked = {}
    for ts, amt, tok, snd, is_c, infra, n, wallets in rows:
        if infra or wallets is None or amt < features.FUNDING_MIN_USD:
            continue
        for w in wallets:
            if w != acct:
                linked.setdefault(w, short_account(snd))
    direct = con.execute("SELECT DISTINCT b FROM funding_edges WHERE a = ? AND kind = 'direct'", [acct]).fetchall()
    for (w,) in direct:
        linked.setdefault(w, "direct transfer")
    flagged = dict(con.execute(f"""SELECT account_id, max(score) FROM position_scores
                                    WHERE account_id IN (SELECT unnest(?)) GROUP BY 1""", [list(linked)]).fetchall()) if linked else {}
    out["wallets_sharing_a_personal_funder"] = [
        {"display_id": short_account(w), "via": via, "highest_alert_score": _num(flagged.get(w), 1)}
        for w, via in list(linked.items())[:8]]
    out["no_incoming_usdc_found"] = not rows
    out["peer_pairs_sharing_a_personal_funder"] = cohort_peer_links(con, acct)
    return out


def cohort_peer_links(con, acct: str) -> list[dict]:
    """Pairs of this position's entry-cohort peers that are funding-linked to each other."""
    rows = con.execute("""
        SELECT DISTINCT e.a, e.b, e.via FROM entry_cohorts ec, funding_edges e
        WHERE ec.account_id = ? AND list_contains(ec.winner_peer_ids, e.a)
          AND list_contains(ec.winner_peer_ids, e.b) AND e.a < e.b""", [acct]).fetchall()
    return [{"wallets": [short_account(a), short_account(b)], "via": short_account(v)} for a, b, v in rows[:5]]


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
