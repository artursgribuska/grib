"""The account-free market screen on the known-cases database: where do the reported cases rank?

    uv run python -m eval.screen_cases        # writes data/known_cases/screen.md

Same universe as eval/known_cases.py, but the screen sees no account ids at all, only each
trade's time, outcome, side, price and size. This is what a venue with an anonymised feed
(Kalshi's public trades) or an operator sharing anonymised data could run.
"""

from __future__ import annotations

import argparse
import json

import duckdb
import pandas as pd

import config
from scoring.market_screen import screen


def event_ranks(con) -> tuple[pd.DataFrame, int, pd.DataFrame]:
    """Per known case: best market rank and best event (story) rank under the screen."""
    s = screen(con)
    s["rank"] = range(1, len(s) + 1)
    ev = con.execute("""SELECT market_id, coalesce(json_extract_string(metadata_json, '$.event_title'), question) AS event
                        FROM markets""").df()
    s = s.merge(ev, on="market_id", how="left")
    e = s.groupby("event").score.max().sort_values(ascending=False)
    erank = pd.Series(range(1, len(e) + 1), index=e.index)
    s["event_rank"] = s.event.map(erank)
    cases = con.execute('SELECT "case", market_id FROM known_case_markets').df()
    c = s.merge(cases, on="market_id").groupby("case").agg(market_rank=("rank", "min"), event_rank=("event_rank", "min"),
                                                           markets=("market_id", "nunique")).reset_index()
    return c.sort_values("event_rank"), len(e), s


def report(con) -> str:
    c, n_events, s = event_ranks(con)
    rows = "\n".join(f"| {r.case} | #{int(r.event_rank)} | #{int(r.market_rank)} | {int(r.markets)} |" for r in c.itertuples())
    top = "\n".join(f"| {i} | {r.score:.1f} | {r.category.replace('_', ' ')} | {r.question} |"
                    for i, r in enumerate(s.head(10).itertuples(), 1))
    return f"""# Market screen on the known cases (no account ids)

{len(s)} markets screened, {n_events} events (stories). The screen sees trades without accounts.

| Case | Event rank | Market rank | Case markets screened |
|---|---:|---:|---:|
{rows}

Top 10 markets:

| # | Score | Category | Market |
|---:|---:|---|---|
{top}

The feature weights were set before this evaluation. The category prior's 10% share mirrors the
position engine; it was added to the screen after seeing that, without it, sports and tweet-count
markets crowd the top (without it the event ranks are 1, 7, 12, 22, 23). The public moment of a story with
several markets is the earliest certainty among siblings with the same result: this was added after a
Kalshi run showed post-news trading in a slower sibling counted as "before public". It lowered the
ceasefire case from #2 to #19, because its earlier rank had counted trading after the 7 April
announcement. Markets flagged here are worth a closer look, not findings.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(config.DATA_DIR / "known_cases.duckdb"))
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db, read_only=True)
    text = report(con)
    c, n, _ = event_ranks(con)
    con.close()
    out = config.DATA_DIR / "known_cases"
    out.mkdir(parents=True, exist_ok=True)
    (out / "screen.md").write_text(text)
    (out / "screen_summary.json").write_text(json.dumps(
        {"events": n, "event_ranks": {r.case: int(r.event_rank) for r in c.itertuples()}}, indent=2))
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
