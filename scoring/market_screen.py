"""Market screen: rank resolved markets by pre-public informed flow, without account ids.

Some venues publish trades without accounts (Kalshi's public feed), and some operators can
share only anonymised trades. The position engine needs accounts; this screen needs only
each trade's time, outcome, side, price and size, so it runs on any venue.

For each resolved market, with c = the moment the outcome became public (est., from prices; for
a story with several markets, such as "out before Oct 20 / Oct 24 / Nov 1", the earliest moment
among siblings with the same result settled within 3 days, so post-news trading in a slower sibling is not counted):

  cheap_flow   buying of the eventual winner at a winner price ≤ 0.35 in the 48 hours before c
               (ending 1 hour before), as a share of all volume traded before c
  rise         how far the winner's price drifted up over those 48 hours, from ≤ 0.60 (no credit
               when a 'No' became certain within 72 hours of its deadline: time ran out in public)
  surge        trading rate in those 48 hours against the market's earlier rate (log ratio)
  one_sided    the winner's share of all buying in those 48 hours

Each feature is turned into a percentile among the screened markets; the flow score is their
weighted average (0–100), and the screen score blends in the category prior at 10%, the same
weight it has in the position engine. High scores mean money moved early and cheaply onto the eventual
winner just before the public moment: a market worth a closer look, not a finding. Markets
whose outcome becomes clear gradually in public (vote counts, live sport) score high for
innocent reasons; the category prior damps them, and the category is shown next to each score.

    uv run python -m scoring.market_screen                  # top markets in the configured database
"""

from __future__ import annotations

import argparse

import duckdb
import pandas as pd

import config
from scoring.features import build_features
from signals.category_prior import CATEGORY_WEIGHTS

WINDOW_H = 48
GAP_H = 1                    # stop the window an hour before the public moment
CHEAP = 0.35
RISE_FROM_MAX = 0.60
MIN_PRE_VOLUME = 5_000
WEIGHTS = {"cheap_flow": 0.35, "rise": 0.25, "surge": 0.20, "one_sided": 0.20}
SIBLING_DAYS = 3             # sibling markets settled within this many days share the story's public moment
DEADLINE_H = 72              # a 'No' certain within this long of its deadline gets no price-drift credit
CATEGORY_SHARE = 0.10        # the category prior's share of the score, the same weight as in the position engine


def features(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """One row per resolved market with enough pre-public volume. Needs trades_w / market_state
    (scoring.features.build_features)."""
    return con.execute(f"""
        WITH ms AS (
            SELECT s.*, m.closes_at,
                   coalesce(json_extract_string(m.metadata_json, '$.event_title'), s.market_id) AS event_key
            FROM market_state s JOIN markets m USING (venue, market_id)),
        pub AS (    -- the story's public moment: the earliest certainty among siblings with the same result, settled together
            SELECT a.venue, a.market_id, least(a.certainty_at, min(b.certainty_at)) AS public_at
            FROM ms a LEFT JOIN ms b ON b.venue = a.venue AND b.event_key = a.event_key
                 AND b.winning_outcome = a.winning_outcome
                 AND abs(epoch(b.resolved_at - a.resolved_at)) <= {SIBLING_DAYS} * 86400
            GROUP BY a.venue, a.market_id, a.certainty_at),
        s AS (
            SELECT ms.venue, ms.market_id, ms.question, ms.category, p.public_at AS certainty_at, ms.first_trade_ts,
                   ms.market_volume_pre,
                   -- a 'No' that became certain close to its deadline ran out of time in public view
                   (ms.winning_outcome = 'No' AND ms.closes_at IS NOT NULL
                    AND epoch(ms.closes_at - p.public_at) <= {DEADLINE_H} * 3600) AS deadline_decay,
                   p.public_at - INTERVAL {WINDOW_H + GAP_H} HOUR AS w0,
                   p.public_at - INTERVAL {GAP_H} HOUR AS w1
            FROM ms JOIN pub p USING (venue, market_id)
            WHERE ms.market_volume_pre >= {MIN_PRE_VOLUME} AND p.public_at > ms.first_trade_ts),
        w AS (
            SELECT s.venue, s.market_id,
                   sum(t.notional / t.fill_rows) FILTER (WHERE t.ts >= s.w0 AND t.ts < s.w1) AS win_vol,
                   sum(t.notional / t.fill_rows) FILTER (WHERE t.ts < s.w0) AS base_vol,
                   sum(t.notional) FILTER (WHERE t.ts >= s.w0 AND t.ts < s.w1 AND t.side = 'buy'
                                           AND t.outcome = t.winning_outcome AND t.price <= {CHEAP}) AS cheap_buy,
                   sum(t.notional) FILTER (WHERE t.ts >= s.w0 AND t.ts < s.w1 AND t.side = 'buy'
                                           AND t.outcome = t.winning_outcome) AS win_buy,
                   sum(t.notional) FILTER (WHERE t.ts >= s.w0 AND t.ts < s.w1 AND t.side = 'buy') AS all_buy,
                   arg_max(t.winner_price, t.ts) FILTER (WHERE t.ts < s.w0 AND t.winner_price IS NOT NULL) AS p_start,
                   arg_min(t.winner_price, t.ts) FILTER (WHERE t.ts >= s.w0 AND t.winner_price IS NOT NULL) AS p_first,
                   arg_max(t.winner_price, t.ts) FILTER (WHERE t.ts < s.w1 AND t.winner_price IS NOT NULL) AS p_end
            FROM s JOIN trades_w t USING (venue, market_id)
            GROUP BY 1, 2)
        SELECT s.venue, s.market_id, s.question, s.category, s.certainty_at, s.market_volume_pre,
               coalesce(w.win_vol, 0) AS window_volume,
               coalesce(w.cheap_buy, 0) / s.market_volume_pre AS cheap_flow,
               s.deadline_decay,
               CASE WHEN NOT s.deadline_decay AND coalesce(w.p_start, w.p_first) <= {RISE_FROM_MAX}
                    THEN greatest(coalesce(w.p_end, 0) - coalesce(w.p_start, w.p_first), 0) ELSE 0 END AS rise,
               CASE WHEN coalesce(w.base_vol, 0) > 0 AND coalesce(w.win_vol, 0) > 0
                         AND epoch(s.w0 - s.first_trade_ts) > 24 * 3600
                    THEN ln((w.win_vol / {WINDOW_H}) / (w.base_vol / (epoch(s.w0 - s.first_trade_ts) / 3600)))
                    ELSE 0 END AS surge,
               CASE WHEN coalesce(w.all_buy, 0) > 0 THEN w.win_buy / w.all_buy ELSE 0 END AS one_sided
        FROM s LEFT JOIN w USING (venue, market_id)""").df()


def screen(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    f = features(con)
    if f.empty:
        return f.assign(score=pd.Series(dtype=float))
    f["score"] = 0.0
    for k, wt in WEIGHTS.items():
        f[f"{k}_pct"] = f[k].rank(pct=True, method="average")
        f["score"] += wt * f[f"{k}_pct"]
    f["flow_score"] = 100 * f.score / sum(WEIGHTS.values())
    prior = f.category.map(CATEGORY_WEIGHTS).fillna(CATEGORY_WEIGHTS["other"])
    f["score"] = (1 - CATEGORY_SHARE) * f.flow_score + 100 * CATEGORY_SHARE * prior
    f.loc[f.window_volume <= 0, ["flow_score", "score"]] = 0.0   # nothing traded before the news: nothing to screen
    return f.sort_values("score", ascending=False).reset_index(drop=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--rebuild", action="store_true", help="rebuild trades_w / market_state first")
    a = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH), read_only=not a.rebuild)
    if a.rebuild:
        build_features(con)
    s = screen(con)
    con.close()
    text = report(s, a.top)
    out = config.OUTPUT_DIR / "market_screen.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(text)
    print(f"(written to {out})")
    return 0


def report(s: pd.DataFrame, top: int = 20) -> str:
    rows = "\n".join(
        f"| {i} | {r.score:.1f} | {r.category.replace('_', ' ')} | {r.question} | {r.cheap_flow:.1%} | {r.rise:+.2f} | "
        f"{r.surge:+.1f} | {r.one_sided:.0%} | ${r.market_volume_pre:,.0f} |"
        for i, r in enumerate(s.head(top).itertuples(), 1))
    return f"""# Market screen

{len(s)} resolved markets screened (≥ ${MIN_PRE_VOLUME:,} traded before the outcome became public). No account
information is used. Window: the {WINDOW_H} hours before the public moment (est.), ending {GAP_H} hour before it.

| # | Score | Category | Market | Cheap winner buying | Winner price drift | Surge (log) | Winner share of buying | Volume before public |
|---:|---:|---|---|---:|---:|---:|---:|---:|
{rows}

*Cheap winner buying*: buying of the eventual winner at ≤ {CHEAP:.2f}, as a share of all volume before the
public moment. A high score means money moved early and cheaply onto the eventual winner shortly before
the outcome became public: a market worth a closer look, not a finding.
"""


if __name__ == "__main__":
    raise SystemExit(main())
