"""Milestone 2: score every candidate position and list the top alerts.

    uv run python -m scoring.run            # top 25
    uv run python -m scoring.run --top 50

Writes four tables:
  * position_scores — one row per candidate position: score, contributions, explanation
  * signal_results  — one row per (position, signal): score, reason, evidence JSON
  * alert_events    — alerts rolled up by event (related markets), most-flagged first
  * alert_wallets   — alerts rolled up by wallet (accounts flagged in several markets)
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

import duckdb
import pandas as pd

import config
from scoring import features
from scoring.features import build_features, candidate_buys
from scoring.score import LABELS, ScoredPosition, score_position, short_account
from signals.base import Buy, PositionContext


def load_contexts(con: duckdb.DuckDBPyConnection) -> list[PositionContext]:
    pos = con.execute("""
        SELECT c.*, s.question, s.category, s.certainty_at, s.resolved_at, s.market_volume,
               d.med_log, d.mad_log, d.n_positions,
               a.first_seen_at, a.metadata_json,
               h.dataset_markets, h.dataset_buy_notional, h.per_market,
               mt.avg_lateness, mt.share_within_72h, mt.lifetime_hours,
               ec.winner_peers, ec.loser_peers, ec.winner_peer_usd
        FROM candidate_positions c
        JOIN market_state s USING (venue, market_id)
        LEFT JOIN market_size_dist d USING (venue, market_id)
        LEFT JOIN accounts a USING (venue, account_id)
        LEFT JOIN account_history h USING (venue, account_id)
        LEFT JOIN market_timing mt USING (venue, market_id)
        LEFT JOIN entry_cohorts ec USING (venue, market_id, account_id)""").df()
    buys = candidate_buys(con)
    legs: dict[tuple, list[Buy]] = defaultdict(list)
    for r in buys.itertuples(index=False):
        legs[(r.venue, r.market_id, r.account_id)].append(
            Buy(r.ts.to_pydatetime(), float(r.price), float(r.size), float(r.notional)))

    out = []
    for r in pos.itertuples(index=False):
        meta = json.loads(r.metadata_json) if isinstance(r.metadata_json, str) else {}
        others = [p["notional"] for p in (r.per_market if r.per_market is not None else [])
                  if p["market_id"] != r.market_id]
        out.append(PositionContext(
            market_id=r.market_id, account_id=r.account_id, outcome=r.outcome,
            question=r.question, category=r.category,
            certainty_at=r.certainty_at.to_pydatetime(), resolved_at=r.resolved_at.to_pydatetime(),
            market_volume=float(r.market_volume),
            buys=legs[(r.venue, r.market_id, r.account_id)],
            account_first_seen_at=r.first_seen_at.to_pydatetime() if pd.notna(r.first_seen_at) else None,
            account_first_seen_source=meta.get("first_seen_source", "dataset_first_trade"),
            lifetime_markets=meta.get("lifetime_markets_traded"),
            dataset_markets=int(r.dataset_markets or 1),
            dataset_buy_notional=float(r.dataset_buy_notional or 0),
            other_position_notionals=others,
            market_log_median=float(r.med_log) if pd.notna(r.med_log) else None,
            market_log_mad=float(r.mad_log) if pd.notna(r.mad_log) else None,
            market_percentile=float(r.market_percentile),
            market_n_positions=int(r.n_positions or 0),
            hedge_notional=float(r.hedge_notional or 0),
            sold_size_pre=float(r.sold_size_pre or 0),
            sold_notional_pre=float(r.sold_notional_pre or 0),
            market_avg_lateness=_f(r.avg_lateness),
            market_share_within_72h=_f(r.share_within_72h),
            market_lifetime_hours=_f(r.lifetime_hours),
            cohort_winner_peers=int(_f(r.winner_peers) or 0),
            cohort_loser_peers=int(_f(r.loser_peers) or 0),
            cohort_winner_peer_usd=float(_f(r.winner_peer_usd) or 0),
        ))
    return out


def _f(x):
    return float(x) if x is not None and pd.notna(x) else None


def save(con: duckdb.DuckDBPyConnection, scored: list[ScoredPosition]) -> None:
    rows, sig_rows = [], []
    for sp in scored:
        c = sp.ctx
        pid = f"{c.market_id[:10]}:{c.account_id}"
        rows.append({
            "position_id": pid, "market_id": c.market_id, "account_id": c.account_id,
            "outcome": c.outcome, "category": c.category, "question": c.question,
            "score": sp.score, "position_notional": round(c.buy_notional, 2),
            "avg_price": round(c.vwap, 4), "first_buy_ts": c.first_buy_ts,
            "certainty_at": c.certainty_at, "resolved_at": c.resolved_at,
            "hedge_ratio": round(sp.hedge_ratio, 4),
            "exited_ratio": round(c.exited_ratio, 4),
            "contributions_json": json.dumps(sp.contributions), "explanation": sp.explanation,
        })
        for s in sp.signals:
            sig_rows.append({"position_id": pid, "signal": s.name, "score": round(s.score, 4),
                             "contribution": sp.contributions[s.name], "reason": s.reason,
                             "evidence_json": json.dumps(s.evidence, default=str)})
    con.register("ps", pd.DataFrame(rows))
    con.execute("CREATE OR REPLACE TABLE position_scores AS SELECT * FROM ps ORDER BY score DESC")
    con.register("sr", pd.DataFrame(sig_rows))
    con.execute("CREATE OR REPLACE TABLE signal_results AS SELECT * FROM sr")
    build_rollups(con)


ROLLUP_MIN_SCORE = 40.0   # alerts near or above the review line feed the roll-ups


def build_rollups(con: duckdb.DuckDBPyConnection) -> None:
    """Group alerts by event (related markets on the same story) and by wallet.

    The event key is the venue's event title where one exists, else the market question,
    so related markets ("…by March 31?", "…before divorce?") land together.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE alert_events AS
    WITH a AS (
        SELECT ps.*, coalesce(json_extract_string(m.metadata_json, '$.event_title'), ps.question) AS event_key,
               coalesce(ap.lifetime_markets, ap.dataset_markets) <= {features.LOW_HISTORY_MAX_MARKETS}
                 OR (ap.first_seen_source = 'venue_join_date'
                     AND ps.first_buy_ts - ap.first_seen_at <= INTERVAL {features.LOW_HISTORY_MAX_AGE_DAYS} DAY) AS low_history
        FROM position_scores ps JOIN markets m USING (market_id)
        LEFT JOIN account_profile ap USING (account_id)
        WHERE ps.score >= {ROLLUP_MIN_SCORE})
    SELECT event_key, count(DISTINCT market_id) AS markets, count(*) AS alerts,
           count(DISTINCT account_id) AS wallets,
           count(DISTINCT account_id) FILTER (WHERE low_history) AS low_history_wallets,
           round(sum(position_notional), 0) AS total_usd, max(score) AS max_score, round(avg(score), 1) AS avg_score,
           list(DISTINCT account_id) AS account_ids
    FROM a GROUP BY 1
    ORDER BY wallets DESC, max_score DESC;

    CREATE OR REPLACE TABLE alert_wallets AS
    SELECT account_id, count(*) AS alerts, count(DISTINCT market_id) AS markets,
           round(sum(position_notional), 0) AS total_usd, max(score) AS max_score,
           list(question ORDER BY score DESC) AS questions
    FROM position_scores WHERE score >= {ROLLUP_MIN_SCORE}
    GROUP BY 1 HAVING count(*) >= 2
    ORDER BY alerts DESC, max_score DESC;
    """)


def run(con: duckdb.DuckDBPyConnection) -> list[ScoredPosition]:
    build_features(con)
    scored = sorted((score_position(c) for c in load_contexts(con)), key=lambda s: -s.score)
    save(con, scored)
    return scored


def print_top(scored: list[ScoredPosition], n: int) -> None:
    print(f"\nTOP {n} ALERTS — anomalous activity warranting review (not findings of wrongdoing)")
    print(f"{len(scored):,} candidate positions scored (winning-side positions ≥ $1,000 built before public certainty)\n")
    keys = list(LABELS)
    abbrev = {"pre_resolution_timing": "TIME", "low_probability_conviction": "LOWP",
              "fresh_single_purpose_wallet": "FRSH", "size_anomaly": "SIZE",
              "category_prior": "CAT", "coordinated_entry": "COORD",
              "hedge_adjustment": "HDG", "exit_adjustment": "EXIT"}
    print(f"{'#':>2} {'score':>5}  " + " ".join(f"{abbrev[k]:>5}" for k in keys) + "  account      market")
    for i, sp in enumerate(scored[:n], 1):
        print(f"{i:>2} {sp.score:>5.1f}  " + " ".join(f"{sp.contributions[k]:>5.1f}" for k in keys)
              + f"  {short_account(sp.ctx.account_id)}  {sp.ctx.question[:60]}")
        print(f"{'':>9}{sp.explanation}")
    print("\nColumns are points contributed by each signal: TIME pre-resolution timing, LOWP low-probability "
          "conviction, FRSH fresh/single-purpose wallet, SIZE size anomaly, CAT category prior, COORD coordinated entry, "
          "HDG hedge adjustment, EXIT sold before public.")


def print_rollups(con: duckdb.DuckDBPyConnection, n: int = 10) -> None:
    ev = con.execute("SELECT * FROM alert_events WHERE wallets >= 2 LIMIT ?", [n]).df()
    print(f"\nEVENTS WITH SEVERAL FLAGGED WALLETS (alerts scoring ≥ {ROLLUP_MIN_SCORE:.0f}, grouped by event)")
    if ev.empty:
        print("  none")
    for r in ev.itertuples():
        print(f"  {r.wallets:>3} wallets ({r.low_history_wallets} low-history) across {r.markets} market(s), "
              f"${r.total_usd:,.0f}, max score {r.max_score:.1f}  {r.event_key[:70]}")
    wl = con.execute("SELECT * FROM alert_wallets LIMIT ?", [n]).df()
    print(f"\nWALLETS FLAGGED IN SEVERAL MARKETS (alerts scoring ≥ {ROLLUP_MIN_SCORE:.0f})")
    if wl.empty:
        print("  none")
    for r in wl.itertuples():
        qs = "; ".join(q[:45] for q in list(r.questions)[:3])
        print(f"  {short_account(r.account_id)}  {r.alerts} alerts, ${r.total_usd:,.0f}, max {r.max_score:.1f}  {qs}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH))
    scored = run(con)
    print_top(scored, args.top)
    print_rollups(con)
    con.close()


if __name__ == "__main__":
    main()
