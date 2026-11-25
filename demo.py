"""Quick demo: the known-cases result in ~10–15 minutes instead of an hour.

    uv run python -m demo                  # 40 ordinary markets + the Maduro and Nobel cases
    uv run python -m demo --markets 80

Builds its own database (data/demo.duckdb) and outputs (data/demo/), never touching the
main data: a smaller ordinary universe, the two single-market publicly reported cases,
the unchanged engine, the case report, the live-watch replay, AI case files for the top
alerts (if an Anthropic key is set) and the dashboard. Rankings are out of a smaller
universe than the full study, so they read "#k of N" for a smaller N.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

DEMO_DB = os.path.join("data", "demo.duckdb")
DEMO_CASES = ("maduro-2026", "nobel-2025")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=int, default=40, help="ordinary markets to load around the cases")
    ap.add_argument("--cases", type=int, default=5, help="AI case files for the top N alerts (needs a key)")
    a = ap.parse_args(argv)

    # the database is chosen when config is first imported, so set it before importing the project
    os.environ["GRIB_DB_PATH"] = os.environ.get("GRIB_DEMO_BASE_DB", os.path.join("data", "demo_base.duckdb"))
    import config
    from eval import known_cases as kc
    from ingest import enrich, run as ingest_run

    t0 = time.time()
    print(f"1/5 loading {a.markets} ordinary markets …", flush=True)
    ingest_run.main(["--markets", str(a.markets)])
    enrich.main([])

    print("2/5 adding the publicly reported cases and scoring …", flush=True)
    kc.DB = config.DATA_DIR / "demo.duckdb"
    kc.OUT = config.DATA_DIR / "demo"
    kc.CASES = [c for c in kc.CASES if c["key"] in DEMO_CASES]
    kc.build(base_db=config.DB_PATH)
    report = kc.report()

    # everything after this point works on the demo database
    config.DB_PATH = kc.DB
    config.OUTPUT_DIR = kc.OUT
    import duckdb

    print("3/5 live-watch replay …", flush=True)
    from eval.watch_replay import report as watch_report
    con = duckdb.connect(str(kc.DB), read_only=True)
    replay = watch_report(con)
    con.close()
    (kc.OUT / "watch_replay.md").write_text(replay)

    print("4/5 case files …", flush=True)
    from cases import run as cases_run
    cases_run.OUT_DIR = kc.OUT / "cases"
    cases_run.main(["--top", str(a.cases)])

    print("5/5 dashboard …", flush=True)
    from app import build
    con = duckdb.connect(str(kc.DB), read_only=True)
    out = build.build(con, out=kc.OUT / "dashboard" / "index.html")
    con.close()

    print("\n" + "\n".join(l for l in report.splitlines() if "best rank" in l or "Event view" in l or l.startswith("## ")))
    print("\n" + "\n".join(l for l in replay.splitlines() if l.startswith("## ") or l.startswith("First")))
    print(f"\nDone in {(time.time() - t0) / 60:.0f} min. Open {out}\n"
          f"Report: {kc.OUT / 'report.md'} · Replay: {kc.OUT / 'watch_replay.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
