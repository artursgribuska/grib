"""Replay the account-free watch (watch/flow.py) on the known-cases database, accounts ignored.

    uv run python -m eval.flow_replay          # → data/known_cases/flow_replay.md

Reported cases: did a flow-surge alert fire on the eventual winner before the outcome became
public, and how early? Ordinary markets: alerts per 1,000 market-days over the 14 days before
each outcome became public, and whether alerted outcomes won more often than their price at
the moment of the alert implied. The thresholds were fixed before this evaluation was first run.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import timedelta

import duckdb
import pandas as pd

import config
from watch import flow
from watch.follow_up import excess_wins

DAYS = 14


def replay(con) -> pd.DataFrame:
    st = con.execute("SELECT market_id, category, winning_outcome, certainty_at FROM market_state").df()
    rows = []
    for r in st.itertuples():
        t = flow.load(con, [r.market_id])
        t = t[t.ts < r.certainty_at]                         # nothing after the news is ever used
        for a in flow.scan(t):
            if a.at < r.certainty_at - timedelta(days=DAYS) or a.at >= r.certainty_at:
                continue
            rows.append({**a.__dict__, "category": r.category, "winner": r.winning_outcome,
                         "on_winner": a.outcome == r.winning_outcome,
                         "hours_before_public": (r.certainty_at - a.at).total_seconds() / 3600})
    return pd.DataFrame(rows), list(st.market_id)


def report(con) -> tuple[str, dict]:
    a, screened = replay(con)
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    cases = con.execute('SELECT "case", market_id FROM known_case_markets').df() if "known_case_markets" in tables \
        else pd.DataFrame(columns=["case", "market_id"])
    lines = ["# Account-free watch: replay", "",
             f"Thresholds (fixed before the first run): a long shot (≤ {flow.MAX_START}) rises ≥ {flow.MIN_RISE} in "
             f"{flow.WINDOW_H} h, with ≥ {flow.MIN_ONE_SIDED:.0%} of the buying, ≥ ${flow.MIN_USD:,} of it, and trading "
             f"≥ {flow.MIN_SURGE:.0f}× the usual rate. No account information is used.", ""]
    res = {"cases": {}}
    lines += ["## Reported cases", "", "| Case | Case markets | First alert on the winner before public | Alerts on other outcomes |",
              "|---|---:|---|---:|"]
    for key, g in cases.groupby("case"):
        ca = a[a.market_id.isin(g.market_id)] if len(a) else a
        w = ca[ca.on_winner].sort_values("hours_before_public", ascending=False) if len(ca) else ca
        first = (f"{w.iloc[0].hours_before_public:.1f} h before ({w.iloc[0].question[:50]})" if len(w) else "none")
        res["cases"][key] = {"first_lead_h": round(float(w.iloc[0].hours_before_public), 1) if len(w) else None,
                             "winner_alerts": int(len(w)), "other_alerts": int((~ca.on_winner).sum()) if len(ca) else 0}
        lines.append(f"| {key} | {len(g)} | {first} | {res['cases'][key]['other_alerts']} |")
    ordinary = a[~a.market_id.isin(cases.market_id)] if len(a) else a
    n_ord = len(set(screened) - set(cases.market_id))
    x = excess_wins(ordinary.assign(won=ordinary.on_winner, avg_price=ordinary.price_at)) if len(ordinary) else \
        {"resolved": 0, "wins": 0, "expected_wins": 0.0, "z": None}
    p = None if x["z"] is None else 0.5 * math.erfc(x["z"] / math.sqrt(2))
    res["ordinary"] = {"markets": int(n_ord), "alerts": int(len(ordinary)),
                       "per_1000_market_days": round(1000 * len(ordinary) / (n_ord * DAYS), 2) if n_ord else None,
                       "wins": x["wins"], "expected_wins": round(x["expected_wins"], 2),
                       "z": None if x["z"] is None else round(x["z"], 2), "p_one_sided": None if p is None else round(p, 3)}
    o = res["ordinary"]
    lines += ["", "## Ordinary markets", "",
              f"{o['alerts']} alerts over {o['markets']} markets × {DAYS} days = **{o['per_1000_market_days']} per 1,000 "
              f"market-days**. The alerted outcome won **{o['wins']}** times; its price at the moment of the alert "
              f"implied {o['expected_wins']} (z = {o['z']}, one-sided p = {o['p_one_sided']}).", "",
              "Alerts are leads for review, not findings.", ""]
    return "\n".join(lines) + "\n", res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(config.DATA_DIR / "known_cases.duckdb"))
    ap.add_argument("--out", default=str(config.DATA_DIR / "known_cases"), help="directory for flow_replay.md/.json")
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db, read_only=True)
    text, res = report(con)
    con.close()
    from pathlib import Path
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "flow_replay.md").write_text(text)
    (out / "flow_replay.json").write_text(json.dumps(res, indent=2))
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
