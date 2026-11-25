"""Milestone 3: build evidence bundles and AI case files for the top alerts.

    uv run python -m cases.run              # top 10 alerts
    uv run python -m cases.run --top 3
    uv run python -m cases.run --bundles-only   # no API key needed

Outputs (in data/cases/): <rank>_<case>.bundle.json, <rank>_<case>.md
and a `case_files` table in the database (used by the dashboard).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import duckdb

import config
from cases.bundle import build_bundle, case_id
from cases.printable import render_html
from cases.writer import MissingApiKey, cost_usd, make_client, render_markdown, write_case

OUT_DIR = config.OUTPUT_DIR / "cases"

DDL = """CREATE TABLE IF NOT EXISTS case_files (
    position_id VARCHAR PRIMARY KEY, created_at TIMESTAMP, model VARCHAR,
    risk_level VARCHAR, risk_confidence VARCHAR, checks_passed BOOLEAN,
    unmatched_numbers JSON, language_problems JSON, attempts INTEGER,
    usage_json JSON, bundle_json JSON, case_json JSON, markdown VARCHAR)"""


def file_stem(rank: int, position_id: str) -> str:
    return f"{rank:02d}_{case_id(position_id)}"


def evidence_fingerprint(bundle: dict) -> str:
    """Hash of everything the case writer sees, except when the bundle was generated."""
    import hashlib
    b = {k: v for k, v in bundle.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(b, sort_keys=True, default=str).encode()).hexdigest()


def prune_stale(con: duckdb.DuckDBPyConnection, ids: list[str], top: int) -> tuple[int, int]:
    """Drop outputs that no longer match the current scores.

    * files for ranks 1..top that belong to a different position than the one now at that rank
    * case_files rows whose position is gone or whose score changed since the case was written
    """
    current = {file_stem(r, pid) for r, pid in enumerate(ids, 1)}
    n_files = 0
    for f in list(OUT_DIR.glob("[0-9][0-9]_CASE-*")):
        stem = f.name.split(".")[0]
        if int(stem[:2]) <= top and stem not in current:
            f.unlink()
            n_files += 1
    stale = [r[0] for r in con.execute("""
        SELECT c.position_id FROM case_files c LEFT JOIN position_scores p USING (position_id)
        WHERE p.position_id IS NULL
           OR round(p.score, 1) <> TRY_CAST(json_extract(c.bundle_json, '$.score.total') AS DOUBLE)""").fetchall()]
    for pid in stale:
        con.execute("DELETE FROM case_files WHERE position_id = ?", [pid])
    return n_files, len(stale)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--bundles-only", action="store_true")
    ap.add_argument("--only-new", action="store_true",
                    help="skip alerts that already have a case file written at their current score")
    args = ap.parse_args(argv)

    con = duckdb.connect(str(config.DB_PATH))
    con.execute("SET enable_progress_bar = false")
    con.execute(DDL)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # same order as the dashboard's review queue: at most PER_EVENT alerts per event, so one surge
    # does not take every case file
    from app.build import PER_EVENT
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "position_events" in tables:
        ids = [r[0] for r in con.execute(
            """SELECT ps.position_id FROM position_scores ps JOIN position_events pe USING (position_id)
               WHERE pe.event_rank <= ? ORDER BY ps.score DESC LIMIT ?""", [PER_EVENT, args.top]).fetchall()]
    else:
        ids = [r[0] for r in con.execute(
            "SELECT position_id FROM position_scores ORDER BY score DESC LIMIT ?", [args.top]).fetchall()]

    removed_files, removed_rows = prune_stale(con, ids, args.top)
    if removed_files or removed_rows:
        print(f"removed {removed_files} outdated file(s) and {removed_rows} outdated case_files row(s)")

    client = None
    if not args.bundles_only:
        try:
            client = make_client()
        except MissingApiKey as e:
            print(f"!! {e}\n   Writing evidence bundles only.", file=sys.stderr)

    existing: dict[str, str] = {}
    if args.only_new:   # position -> fingerprint of the evidence its case file was written from
        existing = {pid: evidence_fingerprint(json.loads(bj))
                    for pid, bj in con.execute("SELECT position_id, bundle_json FROM case_files").fetchall()}
    written, spent = 0, 0.0
    for rank, pid in enumerate(ids, 1):
        bundle = build_bundle(con, pid)
        if existing.get(pid) == evidence_fingerprint(bundle):   # same evidence: re-export under the current rank
            stem = file_stem(rank, pid)
            bj, md, cj, model, unm, lang = con.execute(
                """SELECT bundle_json, markdown, case_json, model, unmatched_numbers, language_problems
                   FROM case_files WHERE position_id = ?""", [pid]).fetchone()
            (OUT_DIR / f"{stem}.bundle.json").write_text(json.dumps(json.loads(bj), indent=1, ensure_ascii=False))
            if md:
                (OUT_DIR / f"{stem}.md").write_text(md)
            if cj:
                (OUT_DIR / f"{stem}.html").write_text(render_html(json.loads(bj), json.loads(cj), {
                    "model": model, "unmatched": json.loads(unm or "[]"), "language": json.loads(lang or "[]")}))
            print(f"{rank:>2}. kept    {stem} (evidence unchanged)")
            continue
        stem = file_stem(rank, pid)
        (OUT_DIR / f"{stem}.bundle.json").write_text(json.dumps(bundle, indent=1, ensure_ascii=False))
        if client is None:   # a case file written from older evidence must not sit next to the new bundle
            (OUT_DIR / f"{stem}.md").unlink(missing_ok=True)
            (OUT_DIR / f"{stem}.html").unlink(missing_ok=True)
            print(f"{rank:>2}. bundle  {stem}  {bundle['market']['question'][:60]}")
            continue
        res = write_case(bundle, client)
        written += 1
        spent += cost_usd(res.usage) or 0.0
        md = render_markdown(bundle, res)
        (OUT_DIR / f"{stem}.md").write_text(md)
        (OUT_DIR / f"{stem}.html").write_text(render_html(bundle, res.case, {
            "model": res.model, "unmatched": res.unmatched_numbers, "language": res.language_problems}))
        con.execute("INSERT OR REPLACE INTO case_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            pid, datetime.now(timezone.utc).replace(tzinfo=None), res.model,
            res.case["risk_rating"]["level"], res.case["risk_rating"]["confidence"], res.checks_passed,
            json.dumps(res.unmatched_numbers), json.dumps(res.language_problems), res.attempts,
            json.dumps(res.usage), json.dumps(bundle, ensure_ascii=False), json.dumps(res.case, ensure_ascii=False), md])
        flag = "ok " if res.checks_passed else "CHECK"
        print(f"{rank:>2}. {flag} {res.case['risk_rating']['level']:<6} attempts={res.attempts} "
              f"tokens in/out={res.usage.get('input_tokens')}/{res.usage.get('output_tokens')}  "
              f"{bundle['market']['question'][:55]}")
        if res.unmatched_numbers:
            print(f"      untraced numbers: {res.unmatched_numbers}")
    con.close()
    print(f"\n{written} case file(s) written to {OUT_DIR}"
          + (f" (≈${spent:.2f} at list prices, ≈${spent / written:.3f} each)" if written else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
