"""Category index: where does anomalous pre-resolution activity concentrate, by market category?

The `category_prior` signal is a hand-set prior (personnel and government markets are more
leak-prone than crypto prices). This report checks that prior against the data without
circularity: each position is re-scored with its category points removed (the *behaviour
score*), and categories are compared on how often behaviour alone crosses the line.

    uv run python -m eval.category_index            # writes <output>/category_index.md

It is also the seed of a public "integrity index". Read it as a description of the sampled
markets, not a measurement of wrongdoing: alerts are leads for review, and ~20 markets per
category is a small sample.
"""

from __future__ import annotations

import argparse
import json

import duckdb
import pandas as pd

import config
from signals.category_prior import CATEGORY_WEIGHTS

TOP_SHARE = 0.01          # default line: the top 1% of behaviour scores, pooled across categories


def behaviour_scores(con) -> pd.DataFrame:
    ps = con.execute("""SELECT market_id, category, score, position_notional, contributions_json
                        FROM position_scores""").df()
    cat_pts = ps.contributions_json.map(lambda s: float(json.loads(s).get("category_prior", 0.0)))
    ps["behaviour"] = ps.score - cat_pts
    return ps.drop(columns="contributions_json")


def category_table(con, line: float | None = None) -> tuple[pd.DataFrame, float]:
    ps = behaviour_scores(con)
    line = float(ps.behaviour.quantile(1 - TOP_SHARE)) if line is None else line
    mk = con.execute("SELECT category, count(*) AS markets FROM markets WHERE resolved_at IS NOT NULL GROUP BY 1").df()
    ps["hit"] = ps.behaviour >= line
    g = ps.groupby("category").agg(positions=("score", "size"), alerts=("hit", "sum"),
                                   markets_with_alert=("market_id", lambda m: m[ps.loc[m.index, "hit"]].nunique()),
                                   flagged_usd=("position_notional", lambda v: v[ps.loc[v.index, "hit"]].sum()),
                                   candidate_usd=("position_notional", "sum")).reset_index()
    peak = ps.groupby(["category", "market_id"]).behaviour.max().groupby("category").median().rename("median_market_peak")
    t = mk.merge(g, on="category", how="left").merge(peak, on="category", how="left").fillna(0)
    t["prior"] = t.category.map(CATEGORY_WEIGHTS).fillna(CATEGORY_WEIGHTS["other"])
    t["share_markets_with_alert"] = t.markets_with_alert / t.markets.where(t.markets > 0)
    t["flagged_usd_share"] = t.flagged_usd / t.candidate_usd.where(t.candidate_usd > 0)
    return t.sort_values("prior", ascending=False).reset_index(drop=True), line


def prior_agreement(t: pd.DataFrame) -> float | None:
    """Spearman rank correlation between the hand-set prior and the median market's strongest
    behaviour score (continuous, so it is not swamped by a handful of alerts). None when there are too few categories to say anything."""
    t = t[t.markets >= 3]
    if len(t) < 4 or t.median_market_peak.nunique() < 2:
        return None
    return float(t.prior.rank().corr(t.median_market_peak.rank()))


def report(con, line: float | None = None) -> str:
    t, line = category_table(con, line)
    rho = prior_agreement(t)
    rows = "\n".join(
        f"| {r.category.replace('_', ' ')} | {r.prior:.2f} | {int(r.markets)} | {int(r.positions)} | {int(r.alerts)} | "
        f"{r.share_markets_with_alert:.0%} | {r.median_market_peak:.1f} | {r.flagged_usd_share:.1%} |"
        for r in t.itertuples())
    agree = ("not enough categories to compare" if rho is None else
             f"Spearman rank correlation between the prior and the median market peak: **{rho:+.2f}** "
             f"({len(t[t.markets >= 3])} categories)")
    return f"""# Category index

Positions re-scored **without** the category prior (behaviour score = score − category points), so
this compares categories on trading behaviour alone. Alert line: behaviour score ≥ {line:.1f}
(the top {TOP_SHARE:.0%} of all positions unless set with `--line`). *Median market peak*: the strongest
behaviour score in each market, median across the category's markets.

| Category | Prior | Markets | Candidate positions | Alerts | Markets with an alert | Median market peak | Flagged $ share |
|---|---:|---:|---:|---:|---:|---:|---:|
{rows}

{agree}.

How to read the agreement: the prior is meant to be *low* where behaviour signals fire for public
reasons. Election counts and live sport make late, confident buying normal, so high behaviour scores
there are expected, and are exactly what the prior damps. A weak or negative correlation therefore
does not by itself mean the prior is wrong; a category with a low prior *and* a few strong, isolated
positions far from the public moment would be the thing to look at.

*Flagged $ share* is the flagged positions' cost as a share of all candidate positions' cost in that
category. Alerts are anomalous activity warranting review, not findings of wrongdoing. With ~20
markets per category, differences of a few alerts are noise.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--line", type=float, default=None, help="behaviour-score line (default: pooled top 1%%)")
    a = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    text = report(con, a.line)
    con.close()
    out = config.OUTPUT_DIR / "category_index.md"
    out.write_text(text)
    print(text)
    print(f"(written to {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
