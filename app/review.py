"""Analyst review loop: record decisions on alerts and learn which signals hold up.

    uv run python -m app.review queue                       # open alerts, highest score first
    uv run python -m app.review decide CASE-99B29C1E78 dismissed --note "public news at 04:50" --by ag
    uv run python -m app.review import decisions.json        # decisions exported from the dashboard
    uv run python -m app.review history CASE-99B29C1E78
    uv run python -m app.review stats                        # how each signal fares against decisions

Decisions are append-only (the latest one per alert counts), so the trail of who decided
what, when and why is kept — which is what an auditor or regulator will ask for.

Decision meanings
  confirmed   the reviewer found the activity warrants action (e.g. account restriction, report)
  dismissed   reviewed and explained (public news, known trader, coincidence …)
  escalated   passed to a senior reviewer / investigation; still open
  needs_info  waiting on KYC, source of funds or other information; still open

`stats` compares confirmed against dismissed alerts per signal. Until there are enough
decisions (MIN_DECISIONS) it says so instead of reporting noise. It suggests, never
applies, weight changes: weights stay a human decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

import config
from cases.bundle import case_id
from scoring.score import ALERT_THRESHOLD, LABELS, WEIGHTS, short_account

DECISIONS = ("confirmed", "dismissed", "escalated", "needs_info")
CLOSED = ("confirmed", "dismissed")
MIN_DECISIONS = 20          # closed decisions needed before stats mean anything
STRONG_AT = 0.70

DDL = """CREATE TABLE IF NOT EXISTS reviews (
    position_id VARCHAR, case_id VARCHAR, decision VARCHAR, note VARCHAR, reviewer VARCHAR,
    decided_at TIMESTAMP, score_at_decision DOUBLE, source VARCHAR)"""

LATEST = """CREATE OR REPLACE TEMP VIEW review_status AS
    SELECT * EXCLUDE (rn) FROM (
        SELECT r.*, row_number() OVER (PARTITION BY position_id ORDER BY decided_at DESC) AS rn FROM reviews r)
    WHERE rn = 1"""


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(config.DB_PATH), read_only=read_only)
    if not read_only:
        con.execute(DDL)
    return con


def ensure(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(DDL)
    con.execute(LATEST)


def resolve(con, ref: str) -> str:
    """Accept a case ID (CASE-…) or a position_id; return the position_id."""
    if ref.upper().startswith("CASE-"):
        for (pid,) in con.execute("SELECT position_id FROM position_scores").fetchall():
            if case_id(pid) == ref.upper():
                return pid
        raise KeyError(f"no alert with case ID {ref}")
    if con.execute("SELECT 1 FROM position_scores WHERE position_id = ?", [ref]).fetchone():
        return ref
    raise KeyError(f"unknown alert {ref}")


def decide(con, ref: str, decision: str, note: str = "", reviewer: str = "", source: str = "cli",
           decided_at: datetime | None = None) -> str:
    ensure(con)
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if decision == "dismissed" and not note.strip():
        raise ValueError("a dismissal needs a note saying why (auditors will ask)")
    pid = resolve(con, ref)
    score = con.execute("SELECT score FROM position_scores WHERE position_id = ?", [pid]).fetchone()[0]
    con.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?)", [
        pid, case_id(pid), decision, note.strip(), reviewer.strip() or "unknown",
        decided_at or datetime.now(timezone.utc).replace(tzinfo=None), score, source])
    return pid


def import_file(con, path: Path, reviewer: str = "") -> tuple[int, list[str]]:
    """Import decisions exported from the dashboard: [{case_id, decision, note, reviewer, decided_at}]."""
    items = json.loads(Path(path).read_text())
    ok, problems = 0, []
    for it in items:
        try:
            at = datetime.fromisoformat(it["decided_at"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None) \
                if it.get("decided_at") else None
            pid = resolve(con, it["case_id"])
            dup = con.execute("""SELECT 1 FROM reviews WHERE position_id = ? AND decision = ? AND decided_at = ?""",
                              [pid, it["decision"], at]).fetchone() if at else None
            if dup:
                continue
            decide(con, it["case_id"], it["decision"], it.get("note", ""), it.get("reviewer") or reviewer,
                   source="dashboard", decided_at=at)
            ok += 1
        except (KeyError, ValueError) as e:
            problems.append(f"{it.get('case_id')}: {e}")
    return ok, problems


def queue(con, limit: int = 25) -> pd.DataFrame:
    ensure(con)
    return con.execute(f"""
        SELECT ps.position_id, ps.score, ps.question, ps.account_id, ps.position_notional,
               coalesce(rs.decision, 'open') AS status
        FROM position_scores ps LEFT JOIN review_status rs USING (position_id)
        WHERE ps.score >= ? AND (rs.decision IS NULL OR rs.decision NOT IN {CLOSED})
        ORDER BY ps.score DESC LIMIT ?""", [ALERT_THRESHOLD - 10, limit]).df()


def stats(con) -> dict:
    """Per signal: share of confirmed vs dismissed alerts where the signal was strong."""
    ensure(con)
    df = con.execute(f"""
        SELECT rs.position_id, rs.decision, sr.signal, sr.score
        FROM review_status rs JOIN signal_results sr USING (position_id)
        WHERE rs.decision IN {CLOSED}""").df()
    n_closed = df.position_id.nunique() if len(df) else 0
    out = {"closed_decisions": int(n_closed), "enough": n_closed >= MIN_DECISIONS, "signals": []}
    if not n_closed:
        return out
    conf = df[df.decision == "confirmed"].position_id.nunique()
    out["confirmed"], out["dismissed"] = int(conf), int(n_closed - conf)
    out["base_rate"] = conf / n_closed
    for sig, g in df.groupby("signal"):
        strong = g[g.score >= STRONG_AT]
        rate = (strong.decision == "confirmed").mean() if len(strong) else None
        lift = rate / out["base_rate"] if rate is not None and out["base_rate"] else None
        out["signals"].append({"signal": sig, "label": LABELS.get(sig, sig), "strong_n": int(len(strong)),
                               "confirm_rate_when_strong": rate, "lift": lift, "weight": WEIGHTS.get(sig)})
    out["signals"].sort(key=lambda s: -(s["lift"] or 0))
    return out


def _print_stats(s: dict) -> None:
    print(f"{s['closed_decisions']} closed decisions", end="")
    if not s["closed_decisions"]:
        print(" — nothing to learn from yet.")
        return
    print(f" ({s['confirmed']} confirmed, {s['dismissed']} dismissed; base confirm rate {s['base_rate']:.0%})")
    if not s["enough"]:
        print(f"Fewer than {MIN_DECISIONS} closed decisions: the figures below are anecdotes, not evidence.")
    print(f"\n{'signal':<32} {'strong in':>9} {'confirmed when strong':>22} {'lift':>6} {'weight':>7}")
    for r in s["signals"]:
        rate = f"{r['confirm_rate_when_strong']:.0%}" if r["confirm_rate_when_strong"] is not None else "—"
        lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
        w = f"{r['weight']:.2f}" if r["weight"] is not None else ""
        print(f"{r['label']:<32} {r['strong_n']:>9} {rate:>22} {lift:>6} {w:>7}")
    if s["enough"]:
        print("\nLift > 1: alerts where this signal is strong are confirmed more often than average — a case for more "
              "weight. Lift < 1: it mostly flags activity reviewers dismiss. Change WEIGHTS in scoring/score.py by hand "
              "and re-run the backtest before adopting.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue"); q.add_argument("--limit", type=int, default=25)
    d = sub.add_parser("decide")
    d.add_argument("ref"); d.add_argument("decision", choices=DECISIONS)
    d.add_argument("--note", default=""); d.add_argument("--by", default="")
    i = sub.add_parser("import"); i.add_argument("path"); i.add_argument("--by", default="")
    h = sub.add_parser("history"); h.add_argument("ref")
    sub.add_parser("stats")
    a = ap.parse_args(argv)

    con = connect()
    if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'position_scores'").fetchone()[0]:
        print("No scored alerts yet: run `uv run python -m pipeline` first.", file=sys.stderr)
        con.close()
        return 1
    try:
        if a.cmd == "queue":
            df = queue(con, a.limit)
            print(f"{len(df)} open alert(s) scoring ≥ {ALERT_THRESHOLD - 10:.0f}\n")
            for r in df.itertuples():
                print(f"  {case_id(r.position_id)}  {r.score:>5.1f}  {r.status:<10} {short_account(r.account_id)}  "
                      f"${r.position_notional:>9,.0f}  {r.question[:60]}")
        elif a.cmd == "decide":
            pid = decide(con, a.ref, a.decision, a.note, a.by)
            print(f"recorded: {case_id(pid)} → {a.decision}")
        elif a.cmd == "import":
            n, problems = import_file(con, Path(a.path), a.by)
            print(f"imported {n} decision(s)")
            for p in problems:
                print(f"  !! {p}")
        elif a.cmd == "history":
            ensure(con)
            pid = resolve(con, a.ref)
            for r in con.execute("""SELECT decided_at, decision, reviewer, note, score_at_decision, source
                                    FROM reviews WHERE position_id = ? ORDER BY decided_at""", [pid]).fetchall():
                print(f"  {r[0]:%Y-%m-%d %H:%M}  {r[1]:<10} by {r[2]:<10} (score {r[4]:.1f}, {r[5]})  {r[3]}")
        elif a.cmd == "stats":
            _print_stats(stats(con))
    except (KeyError, ValueError) as e:
        print(f"!! {e}", file=sys.stderr)
        return 2
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
