"""One command for the whole surveillance run, safe to repeat (e.g. daily).

    uv run python -m pipeline                    # refresh listing, load, enrich, score, funding, cases, dashboard
    uv run python -m pipeline --skip-ingest      # re-score what is already loaded
    uv run python -m pipeline --no-funding --no-cases

Repeat runs are incremental: HTTP and RPC answers are cached on disk, markets already
loaded are kept, wallets already scanned for funding are skipped, and case files are
only written for alerts that are new or whose score changed (so API spend tracks new
alerts, not the size of the database). Each run is recorded in `pipeline_runs`, with
the alerts that are new since the previous run.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone

import duckdb

import config

log = logging.getLogger("pipeline")

RUNS_DDL = """CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id INTEGER, started_at TIMESTAMP, finished_at TIMESTAMP, steps JSON, markets INTEGER,
    positions_scored INTEGER, alerts_at_threshold INTEGER, new_alerts JSON, ok BOOLEAN, error VARCHAR)"""


def _alerts(con) -> set[str]:
    from scoring.score import ALERT_THRESHOLD
    try:
        return {r[0] for r in con.execute(
            "SELECT position_id FROM position_scores WHERE score >= ?", [ALERT_THRESHOLD]).fetchall()}
    except duckdb.CatalogException:
        return set()


def _db():
    return duckdb.connect(str(config.DB_PATH))


def run(args) -> dict:
    steps: list[tuple[str, callable]] = []
    if not args.skip_ingest:
        from ingest import enrich, run as ingest_run
        steps.append(("ingest", lambda: ingest_run.main([] if args.no_refresh else ["--refresh-listing"])))
        steps.append(("enrich", lambda: enrich.main([])))
    from scoring import run as scoring_run
    steps.append(("score", lambda: scoring_run.main(["--top", "10"])))
    if not args.no_funding:
        from ingest import funding
        steps.append(("funding", lambda: funding.main([])))
        steps.append(("rescore", lambda: scoring_run.main(["--top", "10"])))
    if not args.no_news:
        from ingest import news
        steps.append(("news", lambda: news.main([])))
    if not args.no_cases:
        from cases import run as cases_run
        steps.append(("cases", lambda: cases_run.main(["--top", str(args.cases), "--only-new"])))
    from app import build, dossier
    steps.append(("dossiers", lambda: dossier.main([])))     # before the dashboard, which links to them
    steps.append(("dashboard", lambda: build.main([])))

    con = _db()
    con.execute(RUNS_DDL)
    before = _alerts(con)
    run_id = (con.execute("SELECT coalesce(max(run_id), 0) + 1 FROM pipeline_runs").fetchone()[0])
    con.close()   # each step opens its own connection

    started, timings, error = datetime.now(timezone.utc).replace(tzinfo=None), {}, None
    for name, fn in steps:
        t0 = time.time()
        log.info("── %s", name)
        try:
            fn()
        except SystemExit as e:          # a step that stops cleanly (e.g. no API key) should not sink the run
            if e.code not in (0, None):
                error = f"{name}: exited with {e.code}"
                break
        except Exception as e:           # record and stop: later steps depend on earlier ones
            error = f"{name}: {type(e).__name__}: {e}"
            log.exception("step %s failed", name)
            break
        timings[name] = round(time.time() - t0, 1)

    con = _db()
    after = _alerts(con)
    new = sorted(after - before)
    markets = con.execute("SELECT count(*) FROM markets").fetchone()[0]
    scored = con.execute("SELECT count(*) FROM position_scores").fetchone()[0] if after or before else 0
    new_rows = con.execute("""SELECT position_id, round(score, 1), question FROM position_scores
                              WHERE position_id IN (SELECT unnest(?)) ORDER BY score DESC""", [new]).fetchall() if new else []
    con.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?,?,?)", [
        run_id, started, datetime.now(timezone.utc).replace(tzinfo=None), json.dumps(timings), markets, scored,
        len(after), json.dumps([{"position_id": p, "score": s, "market": q} for p, s, q in new_rows]),
        error is None, error])
    digest = write_digest(con, run_id, timings, after, new_rows, error)
    con.close()
    return {"run_id": run_id, "steps_s": timings, "alerts": len(after), "new_alerts": new_rows, "error": error,
            "digest": digest}


def write_digest(con, run_id: int, timings: dict, alerts: set, new_rows: list, error: str | None):
    """A short Markdown summary of the run, for email / chat / a scheduler to forward."""
    from cases.bundle import case_id
    from scoring.score import ALERT_THRESHOLD
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    status = {}
    if "reviews" in tables and alerts:
        status = dict(con.execute("""
            SELECT coalesce(rs.decision, 'open'), count(*) FROM position_scores ps
            LEFT JOIN (SELECT position_id, arg_max(decision, decided_at) AS decision FROM reviews GROUP BY 1) rs
              USING (position_id)
            WHERE ps.score >= ? GROUP BY 1""", [ALERT_THRESHOLD]).fetchall())
    lines = [f"# Integrity Analyst — run {run_id}", "",
             f"*{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC* · {len(alerts)} alert(s) at or above "
             f"{ALERT_THRESHOLD:.0f} · {len(new_rows)} new since the previous run", ""]
    if error:
        lines += [f"**Run stopped early:** {error}", ""]
    if new_rows:
        lines += ["## New alerts", ""]
        for pid, score, q in new_rows:
            lines.append(f"- **{score:.0f}** · {q} · `{case_id(pid)}`")
        lines.append("")
    if status:
        lines += ["## Review status (alerts at or above threshold)", ""]
        lines += [f"- {k.replace('_', ' ')}: {v}" for k, v in sorted(status.items())] + [""]
    lines += [f"Steps (seconds): {', '.join(f'{k} {v}' for k, v in timings.items())}", "",
              "Anomalous activity warranting review — not findings of wrongdoing. Details: dashboard/index.html"]
    out = config.OUTPUT_DIR / "digest" / f"run-{run_id:04d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    out.write_text(text)
    (out.parent / "latest.md").write_text(text)
    if new_rows or error:
        from app.notify import notify
        notify(text)
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-ingest", action="store_true", help="do not load new data; re-score what is loaded")
    ap.add_argument("--no-refresh", action="store_true", help="ingest without re-querying the market listing")
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--no-cases", action="store_true")
    ap.add_argument("--no-news", action="store_true", help="skip the GDELT news search (needs the Anthropic key)")
    ap.add_argument("--cases", type=int, default=10, help="case files for the top N alerts (new/changed only)")
    res = run(ap.parse_args(argv))
    print(f"\nRUN {res['run_id']}: {res['alerts']} alerts at/above threshold, {len(res['new_alerts'])} new since last run")
    for pid, score, q in res["new_alerts"]:
        print(f"  NEW  {score:>5}  {q[:80]}")
    print(f"step timings (s): {res['steps_s']}")
    print(f"digest: {res['digest']}")
    if res["error"]:
        print(f"!! stopped: {res['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
