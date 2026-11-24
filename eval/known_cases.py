"""Known-cases study: does the unchanged engine flag publicly reported episodes?

    uv run python -m eval.known_cases            # build data/known_cases.duckdb, score, write report
    uv run python -m eval.known_cases --report   # re-print the report from the existing database

The planted-trade backtest only shows the engine finds what we told it to look for. This
study runs it on markets that were the subject of public reports of suspected trading on
non-public information, *inside* the ordinary 200-market universe — so case positions
have to out-rank ~7,000 everyday positions with no special treatment. Nothing about the
cases (names, times, wallets) is given to the engine; it sees only trades.

Each case lists the event and the market(s) whose winning outcome is in question. Full
trade history is loaded for those markets (the demo's per-market row cap would cut off
the older, pre-announcement trades that matter here).

The report states, per case, where the engine ranked the case market's positions among
all positions, and for the top ones: when they were bought relative to the estimated
public moment, at what price, and which signals drove the score. It is evidence about
the engine, not about any person: the public reports are allegations, and the accounts
are pseudonymous.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil

import duckdb
import pandas as pd

import config
from ingest.http import CachedClient
from ingest.polymarket import DataApiClient
from schema import polymarket as pm

log = logging.getLogger("known_cases")

DB = config.DATA_DIR / "known_cases.duckdb"
OUT = config.DATA_DIR / "known_cases"
MAX_ROWS = 600_000

CASES = [
    {"key": "nobel-2025", "event": "nobel-peace-prize-winner-2025", "match": ["Machado"],
     "about": "2025 Nobel Peace Prize: the winner's price rose sharply in the hours before the announcement, "
              "and the Norwegian Nobel Institute said it would look into a possible leak."},
    {"key": "google-person-2025", "event": "1-searched-person-on-google-this-year", "match": ["d4vd"],
     "about": "Google Year in Search 2025: reports described accounts winning several 'most searched' markets "
              "shortly before Google published its lists."},
    {"key": "google-passings-2025", "event": "1-searched-passings-on-google-this-year", "match": ["Charlie Kirk"],
     "about": "Google Year in Search 2025 (passings list), same reporting as above."},
    {"key": "maduro-2026", "event": "maduro-out-in-2025", "match": ["January 31"],
     "about": "Maduro removed from power in January 2026: reports described a recently created account buying "
              "'Maduro out' shortly before the US operation became public."},
]


def gamma_event(http: CachedClient, slug: str) -> dict | None:
    r = http.get_json(f"{config.GAMMA_API}/events", {"slug": slug})
    return r[0] if r else None


def build(base_db=config.DB_PATH) -> pd.DataFrame:
    """Copy the base universe, add the case markets with full trade history, enrich, score."""
    from ingest.enrich import enrich
    from ingest.run import rebuild_accounts
    from scoring.features import build_features
    from scoring.run import run as score_run

    OUT.mkdir(parents=True, exist_ok=True)
    tmp = DB.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    shutil.copyfile(base_db, tmp)
    con = duckdb.connect(str(tmp))
    http = CachedClient()
    api = DataApiClient(http)
    rows = []
    for case in CASES:
        ev = gamma_event(http, case["event"])
        if not ev:
            log.warning("event %s not found", case["event"])
            continue
        for m in ev.get("markets") or []:
            q = m.get("question") or ""
            if not any(s.lower() in q.lower() for s in case["match"]):
                continue
            mk = pm.map_market(m, ev)
            if not mk["winning_outcome"]:
                log.warning("%s: no clean winner, skipped", q)
                continue
            raw = list(api.iter_market_trades(mk["market_id"], max_rows=MAX_ROWS))
            trades = pm.map_trades(raw, mk["market_id"])
            con.execute("DELETE FROM markets WHERE market_id = ?", [mk["market_id"]])
            con.execute("DELETE FROM trades WHERE market_id = ?", [mk["market_id"]])
            con.register("_m", pd.DataFrame([mk]))
            con.execute("INSERT INTO markets SELECT * FROM _m")
            con.unregister("_m")
            if trades:
                con.register("_t", pd.DataFrame(trades))
                con.execute("INSERT INTO trades SELECT * FROM _t")
                con.unregister("_t")
            rows.append({"case": case["key"], "market_id": mk["market_id"], "question": q,
                         "winner": mk["winning_outcome"], "trades": len(trades), "truncated": len(raw) >= MAX_ROWS})
            log.info("%s: %s — %d trades%s", case["key"], q[:60], len(trades), " (TRUNCATED)" if len(raw) >= MAX_ROWS else "")
    cases = pd.DataFrame(rows)
    con.register("_c", cases)
    con.execute("CREATE OR REPLACE TABLE known_case_markets AS SELECT * FROM _c")
    con.unregister("_c")
    rebuild_accounts(con)
    build_features(con)
    wallets = [r[0] for r in con.execute("""SELECT DISTINCT account_id FROM candidate_positions
                                            WHERE market_id IN (SELECT market_id FROM known_case_markets)""").fetchall()]
    log.info("enriching %d case-market wallets", len(wallets))
    enrich(con, wallets)
    score_run(con)
    con.close()
    tmp.replace(DB)
    return cases


def report() -> str:
    from scoring.score import ALERT_THRESHOLD, LABELS, short_account
    con = duckdb.connect(str(DB), read_only=True)
    total = con.execute("SELECT count(*) FROM position_scores").fetchone()[0]
    ranked = con.execute("""SELECT *, row_number() OVER (ORDER BY score DESC) AS rank FROM position_scores""").df()
    cases = con.execute("SELECT * FROM known_case_markets").df()
    lines = ["# Known-cases study", "",
             f"Case markets were scored inside the ordinary universe: **{total:,} positions** in total. "
             f"Rank 1 is the engine's top alert. Review threshold: {ALERT_THRESHOLD:.0f}.", "",
             "These markets were the subject of public reports of suspected trading on non-public information. "
             "The reports are allegations; accounts are pseudonymous and shown truncated. The engine was given "
             "trades only — no names, times or wallets.", ""]
    top25 = ranked[ranked["rank"] <= 25]
    n_case_top25 = top25.market_id.isin(cases.market_id).sum()
    lines += [f"**{n_case_top25} of the top 25 alerts** come from the {len(cases)} case market(s) "
              f"(which hold {ranked.market_id.isin(cases.market_id).sum():,} of the {total:,} positions).", ""]
    for case in CASES:
        cm = cases[cases.case == case["key"]]
        if cm.empty:
            continue
        lines += [f"## {case['key']}", "", case["about"], ""]
        for m in cm.itertuples():
            pos = ranked[ranked.market_id == m.market_id].sort_values("rank")
            st = con.execute("SELECT certainty_at, resolved_at FROM market_state WHERE market_id = ?", [m.market_id]).fetchone()
            lines += [f"**{m.question}** — winner '{m.winner}', {m.trades:,} trades"
                      + (" (history truncated)" if m.truncated else "")
                      + (f"; outcome estimated public at {st[0]:%Y-%m-%d %H:%M} UTC" if st else ""), ""]
            if pos.empty:
                lines += ["No candidate positions (winning-side, ≥ $1,000, before the public moment).", ""]
                continue
            at = (pos.score >= ALERT_THRESHOLD).sum()
            lines += [f"{len(pos)} candidate positions; {at} at or above the threshold; best rank **{int(pos['rank'].min())}** "
                      f"of {total:,}.", "",
                      "| Rank | Score | Account | Cost | Avg price | First buy | Hours before public | Top signals |",
                      "|---|---|---|---|---|---|---|---|"]
            for p in pos.head(8).itertuples():
                contrib = json.loads(p.contributions_json)
                tops = ", ".join(f"{LABELS[k]} {v:.0f}" for k, v in sorted(contrib.items(), key=lambda kv: -kv[1])[:3] if v > 0)
                lead = (p.certainty_at - p.first_buy_ts).total_seconds() / 3600
                lines.append(f"| {int(p.rank)} | {p.score:.1f} | {short_account(p.account_id)} | ${p.position_notional:,.0f} | "
                             f"{p.avg_price:.2f} | {p.first_buy_ts:%Y-%m-%d %H:%M} | {lead:,.1f} | {tops} |")
            lines.append("")
    con.close()
    text = "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.md").write_text(text)
    return text


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="only re-print the report")
    a = ap.parse_args(argv)
    if not a.report:
        build()
    print(report())
    print(f"(written to {OUT / 'report.md'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
