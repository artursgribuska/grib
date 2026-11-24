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
from cases.bundle import build_bundle
from cases.writer import MissingApiKey, make_client, render_markdown, write_case

OUT_DIR = config.DATA_DIR / "cases"

DDL = """CREATE TABLE IF NOT EXISTS case_files (
    position_id VARCHAR PRIMARY KEY, created_at TIMESTAMP, model VARCHAR,
    risk_level VARCHAR, risk_confidence VARCHAR, checks_passed BOOLEAN,
    unmatched_numbers JSON, language_problems JSON, attempts INTEGER,
    usage_json JSON, bundle_json JSON, case_json JSON, markdown VARCHAR)"""


def file_stem(rank: int, position_id: str) -> str:
    return f"{rank:02d}_{position_id.split(':')[0][2:10]}_{position_id[-4:]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--bundles-only", action="store_true")
    args = ap.parse_args(argv)

    con = duckdb.connect(str(config.DB_PATH))
    con.execute("SET enable_progress_bar = false")
    con.execute(DDL)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ids = [r[0] for r in con.execute(
        "SELECT position_id FROM position_scores ORDER BY score DESC LIMIT ?", [args.top]).fetchall()]

    client = None
    if not args.bundles_only:
        try:
            client = make_client()
        except MissingApiKey as e:
            print(f"!! {e}\n   Writing evidence bundles only.", file=sys.stderr)

    for rank, pid in enumerate(ids, 1):
        bundle = build_bundle(con, pid)
        stem = file_stem(rank, pid)
        (OUT_DIR / f"{stem}.bundle.json").write_text(json.dumps(bundle, indent=1, ensure_ascii=False))
        if client is None:
            print(f"{rank:>2}. bundle  {stem}  {bundle['market']['question'][:60]}")
            continue
        res = write_case(bundle, client)
        md = render_markdown(bundle, res)
        (OUT_DIR / f"{stem}.md").write_text(md)
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
    print(f"\nFiles written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
