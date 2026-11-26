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


def ordinary_markets(con, exclude: set[str] | None = None) -> pd.DataFrame:
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    excl = "WHERE market_id NOT IN (SELECT market_id FROM known_case_markets)" if "known_case_markets" in tables else ""
    m = con.execute(f"SELECT market_id, category FROM market_state {excl}").df()
    return m[~m.market_id.isin(exclude)] if exclude else m


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


def first_per_market(a: pd.DataFrame) -> pd.DataFrame:
    """One alert per market (the earliest): alerts on the same market are not independent draws."""
    return a.sort_values("at").groupby("market_id", as_index=False).head(1) if len(a) else a


def summarise(a: pd.DataFrame, n_markets: int) -> dict:
    x = excess_wins(a.assign(won=a.on_winner)) if len(a) else {"resolved": 0, "wins": 0, "expected_wins": 0.0, "z": None}
    f = first_per_market(a)
    y = excess_wins(f.assign(won=f.on_winner)) if len(f) else {"resolved": 0, "wins": 0, "expected_wins": 0.0, "z": None}
    return {"markets": n_markets, "alerts": int(len(a)),
            "per_1000_market_days": round(1000 * len(a) / (n_markets * DAYS), 2) if n_markets else None,
            "wins": x["wins"], "expected_wins": round(x["expected_wins"], 2),
            "z": None if x["z"] is None else round(x["z"], 2), "p_one_sided": None if x["z"] is None else round(p_value(x["z"]), 3),
            "per_market": {"markets_alerted": int(len(f)), "wins": y["wins"], "expected_wins": round(y["expected_wins"], 2),
                           "z": None if y["z"] is None else round(y["z"], 2),
                           "p_one_sided": None if y["z"] is None else round(p_value(y["z"]), 3)},
            "by_kind": {k: {"alerts": int(len(g)), "wins": int(g.on_winner.sum()), "implied": round(float(g.avg_price.sum()), 2)}
                        for k, g in a.groupby("kind")} if len(a) else {}}


def report(con, exclude: set[str] | None = None) -> tuple[str, dict]:
    m = ordinary_markets(con, exclude)
    a = replay_all(con, m)
    if len(a):                                             # kept so the analysis can be redone without a replay
        a.to_csv(config.OUTPUT_DIR / "watch_volume_alerts.csv", index=False)
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
    pm = res["watch_scope"]["per_market"]
    lines += ["", f"Counting only the first alert on each market (alerts on one market are not independent): "
                  f"{pm['markets_alerted']} markets, the alerted side won {pm['wins']} vs {pm['expected_wins']} implied "
                  f"(z = {pm['z']}, one-sided p = {pm['p_one_sided']}). Watch scope."]
    lines += ["", "By kind (watch scope):", ""]
    for k, v in res["watch_scope"]["by_kind"].items():
        lines.append(f"- {k.replace('_', ' ')}: {v['alerts']} alerts, the alerted side won {v['wins']}, prices implied {v['implied']}")
    lines += ["", "*z* compares wins with the wins implied by the prices paid (Poisson-binomial variance). A win is not "
              "evidence against any account; the question is whether alerts, taken together, carry information.", ""]
    return "\n".join(lines) + "\n", res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exclude-db", action="append", default=[],
                    help="leave out markets present in this database (e.g. earlier samples), for an out-of-sample test")
    ap.add_argument("--name", default="watch_volume")
    a = ap.parse_args(argv)
    exclude = set()
    for path in a.exclude_db:
        c = duckdb.connect(path, read_only=True)
        exclude |= {r[0] for r in c.execute("SELECT market_id FROM markets").fetchall()}
        c.close()
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    text, res = report(con, exclude or None)
    con.close()
    if exclude:
        text = text.replace("# Live watch on ordinary markets", f"# Live watch on ordinary markets (excluding {len(exclude)} "
                            "markets from earlier samples)", 1)
    out = config.OUTPUT_DIR / f"{a.name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    (config.OUTPUT_DIR / f"{a.name}.json").write_text(json.dumps(res, indent=2))
    print(text)
    print(f"(written to {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
