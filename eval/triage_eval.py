"""Does the AI first look carry information? Triage historical alerts blind, then check outcomes.

    GRIB_DB_PATH=data/watch_eval.duckdb uv run python -m eval.triage_eval     # needs the API key (~1 cent per alert)

Takes the replayed live-watch alerts of a database (eval/watch_volume.py saves them), and for
each one builds a point-in-time copy of what a reviewer would have seen at the moment of the alert:
the market as open (no outcome), its trades up to that moment, and the account's venue record.
watch/triage.py then drafts its note. Afterwards, per suggested action, we compare how often the
alerted side won with how often its price implied. If "escalate" beats its odds by more than
"close", the triage adds information.

Caveat: the venue record (lifetime markets, volume, profit) is as of today, not as of the alert,
the one piece of hindsight the note can see. Small samples: read the counts, not just the rates.
"""

from __future__ import annotations

import argparse
import json

import duckdb
import pandas as pd

import config
from schema.canonical import create_tables
from watch import triage
from watch.follow_up import excess_wins
from watch.run_live import ALERTS_DDL, SKIP_CATEGORIES


def point_in_time(src, a) -> duckdb.DuckDBPyConnection:
    """An in-memory live database as it stood when alert `a` fired."""
    con = duckdb.connect()
    create_tables(con)
    con.execute(ALERTS_DDL)
    m = src.execute("SELECT * FROM markets WHERE market_id = ?", [a.market_id]).df()
    m["resolved_at"], m["winning_outcome"] = None, None
    con.register("_m", m)
    con.execute("INSERT INTO markets SELECT * FROM _m")
    t = src.execute("SELECT * FROM trades WHERE market_id = ? AND ts <= ?", [a.market_id, a.at]).df()
    con.register("_t", t)
    con.execute("INSERT INTO trades SELECT * FROM _t")
    acc = src.execute("SELECT * FROM accounts WHERE account_id = ?", [a.account_id]).df()
    con.register("_a", acc)
    con.execute("INSERT INTO accounts SELECT * FROM _a")
    con.execute("INSERT INTO watch_alerts VALUES (?,?,?,?,?,?,?,?,?,?)",
                [a.kind, a.market_id, a.question, a.outcome, a.account_id, a.at, a.at, a.usd, a.avg_price, a.detail])
    return con


def run(src, alerts: pd.DataFrame, client=None) -> pd.DataFrame:
    client = client or triage.make_client()
    rows = []
    for a in alerts.itertuples():
        con = point_in_time(src, a)
        triage.run(con, client)
        n = triage.get_note(con, a.kind, a.market_id, a.outcome, a.account_id)
        con.close()
        if n:
            rows.append({"market_id": a.market_id, "kind": a.kind, "action": n["suggested_action"], "passed": n["passed"],
                         "on_winner": bool(a.on_winner), "avg_price": float(a.avg_price)})
    return pd.DataFrame(rows)


def report(r: pd.DataFrame) -> tuple[str, dict]:
    res = {}
    lines = ["# AI first look on historical alerts", "",
             "Each alert was triaged blind, with only the trades up to the moment it fired (venue records are as of "
             "today). Then: how often did the alerted side win, against what its price implied?", "",
             "| Suggested action | Alerts | Won | Implied by prices | Won minus implied | exact p (one-sided) |",
             "|---|---:|---:|---:|---:|---:|"]
    for act in triage.ACTIONS[::-1]:
        g = r[r.action == act]
        if g.empty:
            res[act] = {"alerts": 0}
            lines.append(f"| {act} | 0 | — | — | — | — |")
            continue
        x = excess_wins(g.assign(won=g.on_winner))
        res[act] = {"alerts": int(len(g)), "wins": x["wins"], "implied": round(x["expected_wins"], 2),
                    "excess": round(x["wins"] - x["expected_wins"], 2), "p": round(x["p_exact"], 3)}
        lines.append(f"| {act} | {len(g)} | {x['wins']} | {x['expected_wins']:.1f} | {x['wins'] - x['expected_wins']:+.1f} | "
                     f"{x['p_exact']:.3f} |")
    lines += ["", f"Notes that passed the number and language checks: {int(r.passed.sum())} of {len(r)}.", ""]
    return "\n".join(lines) + "\n", res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--alerts", default=None, help="CSV of replayed alerts (default: <output>/watch_volume_alerts.csv)")
    a = ap.parse_args(argv)
    path = a.alerts or config.OUTPUT_DIR / "watch_volume_alerts.csv"
    alerts = pd.read_csv(path, parse_dates=["at"])
    alerts = alerts[~alerts.category.isin(SKIP_CATEGORIES)]
    src = duckdb.connect(str(config.DB_PATH), read_only=True)
    r = run(src, alerts)
    src.close()
    text, res = report(r)
    (config.OUTPUT_DIR / "triage_eval.md").write_text(text)
    (config.OUTPUT_DIR / "triage_eval.json").write_text(json.dumps(res, indent=2))
    r.to_csv(config.OUTPUT_DIR / "triage_eval_rows.csv", index=False)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
