"""Live watch on ordinary markets: how many alerts, and do flagged sides win more than the odds said?

    GRIB_DB_PATH=data/watch_eval.duckdb uv run python -m eval.watch_volume     # → <output>/watch_volume.md

Replays the live watch over every resolved market in the database (except known-case markets,
if the table exists), every 6 hours over the 14 days before each outcome became public, using
only the trades available at each moment. Then compares how often the alerted side won with
the chance its price implied: the honest test of whether alerts carry information.

The live watch skips sports and short crypto-price markets; results are reported for that
scope (what a team would actually receive) and for all markets.
"""

from __future__ import annotations

import argparse
import json
import math

import duckdb
import pandas as pd

import config
from eval.watch_replay import DAYS, _replay_markets
from watch.follow_up import excess_wins
from watch.live import _account_facts
from watch.run_live import SKIP_CATEGORIES

STEP_H = 6


def ordinary_markets(con) -> pd.DataFrame:
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    excl = "WHERE market_id NOT IN (SELECT market_id FROM known_case_markets)" if "known_case_markets" in tables else ""
    return con.execute(f"SELECT market_id, category FROM market_state {excl}").df()


def replay_all(con, markets: pd.DataFrame) -> pd.DataFrame:
    facts = _account_facts(con)
    seen = con.execute("SELECT account_id, count(DISTINCT market_id) n FROM trades GROUP BY 1").df() \
        .set_index("account_id").n
    a = _replay_markets(con, list(markets.market_id), STEP_H, facts, seen)
    if a.empty:
        return a
    a = a[a.hours_before_public > 0]
    return a.merge(markets, on="market_id", how="left")


def p_value(z: float | None) -> float | None:
    """One-sided p for 'flagged sides win more often than implied' (normal approximation)."""
    return None if z is None else 0.5 * math.erfc(z / math.sqrt(2))


def summarise(a: pd.DataFrame, n_markets: int) -> dict:
    x = excess_wins(a.assign(won=a.on_winner)) if len(a) else {"resolved": 0, "wins": 0, "expected_wins": 0.0, "z": None}
    return {"markets": n_markets, "alerts": int(len(a)),
            "per_1000_market_days": round(1000 * len(a) / (n_markets * DAYS), 2) if n_markets else None,
            "wins": x["wins"], "expected_wins": round(x["expected_wins"], 2),
            "z": None if x["z"] is None else round(x["z"], 2), "p_one_sided": None if x["z"] is None else round(p_value(x["z"]), 3),
            "by_kind": {k: {"alerts": int(len(g)), "wins": int(g.on_winner.sum()), "implied": round(float(g.avg_price.sum()), 2)}
                        for k, g in a.groupby("kind")} if len(a) else {}}


def report(con) -> tuple[str, dict]:
    m = ordinary_markets(con)
    a = replay_all(con, m)
    scope_m = m[~m.category.isin(SKIP_CATEGORIES)]
    res = {"watch_scope": summarise(a[a.market_id.isin(scope_m.market_id)] if len(a) else a, len(scope_m)),
           "all_markets": summarise(a, len(m))}
    lines = ["# Live watch on ordinary markets", "",
             f"Replayed every {STEP_H} h over the {DAYS} days before each outcome became public, using only the "
             "trades available at each moment. Alerts are leads for review, not findings.", "",
             "| Scope | Markets | Alerts | Per 1,000 market-days | Flagged side won | Implied by prices | z | p (one-sided) |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, label in (("watch_scope", "What the watch covers (no sports, no crypto prices)"), ("all_markets", "All markets")):
        r = res[name]
        lines.append(f"| {label} | {r['markets']} | {r['alerts']} | {r['per_1000_market_days']} | {r['wins']} | "
                     f"{r['expected_wins']} | {r['z'] if r['z'] is not None else '—'} | "
                     f"{r['p_one_sided'] if r['p_one_sided'] is not None else '—'} |")
    lines += ["", "By kind (watch scope):", ""]
    for k, v in res["watch_scope"]["by_kind"].items():
        lines.append(f"- {k.replace('_', ' ')}: {v['alerts']} alerts, the alerted side won {v['wins']}, prices implied {v['implied']}")
    lines += ["", "*z* compares wins with the wins implied by the prices paid (Poisson-binomial variance). A win is not "
              "evidence against any account; the question is whether alerts, taken together, carry information.", ""]
    return "\n".join(lines) + "\n", res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    text, res = report(con)
    con.close()
    out = config.OUTPUT_DIR / "watch_volume.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    (config.OUTPUT_DIR / "watch_volume.json").write_text(json.dumps(res, indent=2))
    print(text)
    print(f"(written to {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
