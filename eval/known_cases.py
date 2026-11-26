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

GOOGLE_2025 = ["1-searched-person-on-google-this-year", "1-searched-passings-on-google-this-year",
               "1-searched-actor-on-google-this-year", "2-searched-person-on-google-this-year",
               "1-searched-news-on-google-this-year-385", "1-searched-athlete-on-google-this-year",
               "1-searched-movie-on-google-this-year", "1-searched-song-on-google-this-year",
               "1-searched-tv-show-on-google-this-year", "1-searched-game-on-google-this-year"]

# "events": Gamma event slugs; "match": substrings selecting markets (None = every market of the
# events, losers included — needed when the pattern is many wins across sibling markets).
CASES = [
    {"key": "nobel-2025", "events": ["nobel-peace-prize-winner-2025"], "match": ["Machado"],
     "about": "2025 Nobel Peace Prize: the winner's price rose sharply in the hours before the announcement, "
              "and the Norwegian Nobel Institute said it would look into a possible leak."},
    {"key": "google-yis-2025", "events": GOOGLE_2025, "match": None,
     "about": "Google Year in Search 2025: reports described an account winning many 'most searched' markets "
              "shortly before Google published its lists. All markets of the ten list events are loaded, so an "
              "account's wins and losses across sibling markets are both visible."},
    {"key": "maduro-2026", "events": ["maduro-out-in-2025"], "match": ["January 31"],
     "about": "Maduro removed from power in January 2026. The CFTC's first insider-trading complaint on event "
              "contracts (SDNY 26-cv-3369, April 2026) alleges the purchase of more than 436,000 'Yes' shares of "
              "this market between December 2025 and January 2026 on misappropriated non-public information."},
    {"key": "us-strikes-iran-2026-02", "events": ["us-strikes-iran-by", "us-next-strikes-iran-on-312"],
     "match": ["February 28, 2026", "March 1, 2026", "next strike Iran"],
     "about": "US strikes on Iran, late February 2026: Bubblemaps reported (May 2026) a group of linked wallets that "
              "made about $2.4M on bets tied to US military action, mostly against Iran, with a very high win rate. "
              "Added after the engine was frozen: an out-of-sample test."},
    {"key": "iran-ceasefire-2026-04", "events": ["us-x-iran-ceasefire-by"], "match": ["April 7", "April 10", "April 15"],
     "about": "US–Iran ceasefire announced in April 2026: a member of Congress asked the CFTC to investigate trades "
              "placed shortly before the announcement, and Bubblemaps reported a cluster of linked accounts that made "
              "about $1.4M on a ceasefire bet. Added after the engine was frozen: an out-of-sample test."},
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
    for case, ev in ((c, gamma_event(http, slug)) for c in CASES for slug in c["events"]):
        if not ev:
            log.warning("an event of %s was not found", case["key"])
            continue
        for m in ev.get("markets") or []:
            q = m.get("question") or ""
            if case["match"] and not any(s.lower() in q.lower() for s in case["match"]):
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
            log.debug("%s: %s — %d trades%s", case["key"], q[:60], len(trades), " (TRUNCATED)" if len(raw) >= MAX_ROWS else "")
        log.info("%s: event %s loaded", case["key"], ev.get("slug"))
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
    summary = {"positions": int(total), "cases": []}
    top25 = ranked[ranked["rank"] <= 25]
    n_case_top25 = top25.market_id.isin(cases.market_id).sum()
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "position_events" in tables:
        ev = con.execute("""
            WITH e AS (SELECT pe.event_key, max(ps.score) AS best FROM position_scores ps
                       JOIN position_events pe USING (position_id) GROUP BY 1),
                 r AS (SELECT event_key, rank() OVER (ORDER BY best DESC) AS rank FROM e)   -- ties share a rank
            SELECT k."case", min(r.rank) AS rank, (SELECT count(*) FROM e) AS n
            FROM known_case_markets k JOIN position_scores ps USING (market_id)
            JOIN position_events pe USING (position_id) JOIN r USING (event_key)
            GROUP BY 1 ORDER BY 2""").df()
        if len(ev):
            n_events = int(ev.n.iloc[0])
            lines += ["**Event view** (one row per story, ranked by its strongest position): "
                      + ", ".join(f"{c} #{int(r)}" for c, r in zip(ev["case"], ev["rank"]))
                      + f" of {n_events} events.", ""]
            summary["event_ranks"] = [int(r) for r in ev["rank"]]
            summary["events"] = n_events
    lines += [f"**{n_case_top25} of the top 25 alerts** come from the {len(cases)} case market(s) "
              f"(which hold {ranked.market_id.isin(cases.market_id).sum():,} of the {total:,} positions).", ""]
    for case in CASES:
        cm = cases[cases.case == case["key"]]
        if cm.empty:
            continue
        lines += [f"## {case['key']}", "", case["about"], ""]
        pos = ranked[ranked.market_id.isin(cm.market_id)].sort_values("rank")
        n_trades = int(cm.trades.sum())
        if len(cm) == 1:
            m = cm.iloc[0]
            st = con.execute("SELECT certainty_at FROM market_state WHERE market_id = ?", [m.market_id]).fetchone()
            lines += [f"**{m.question}** — winner '{m.winner}', {n_trades:,} trades"
                      + (" (history truncated)" if m.truncated else "")
                      + (f"; outcome estimated public at {st[0]:%Y-%m-%d %H:%M} UTC" if st else ""), ""]
        else:
            lines += [f"**{len(cm)} markets** loaded ({n_trades:,} trades"
                      + (f"; {int(cm.truncated.sum())} truncated" if cm.truncated.any() else "") + ").", ""]
        if pos.empty:
            lines += ["No candidate positions (winning-side, ≥ $1,000, before the public moment).", ""]
            continue
        at = (pos.score >= ALERT_THRESHOLD).sum()
        summary["cases"].append({"case": case["key"], "markets": int(len(cm)), "candidates": int(len(pos)),
                                 "at_threshold": int(at), "best_rank": int(pos["rank"].min()),
                                 "about": case["about"]})
        lines += [f"{len(pos)} candidate positions; {at} at or above the threshold; best rank **{int(pos['rank'].min())}** "
                  f"of {total:,}.", "",
                  "| Rank | Score | Account | Market | Cost | Avg price | First buy | Hours before public | Top signals |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for p in pos.head(8).itertuples():
            contrib = json.loads(p.contributions_json)
            tops = ", ".join(f"{LABELS[k]} {v:.0f}" for k, v in sorted(contrib.items(), key=lambda kv: -kv[1])[:3] if v > 0)
            lead = (p.certainty_at - p.first_buy_ts).total_seconds() / 3600
            lines.append(f"| {int(p.rank)} | {p.score:.1f} | {short_account(p.account_id)} | {p.question[:45]} | "
                         f"${p.position_notional:,.0f} | {p.avg_price:.2f} | {p.first_buy_ts:%Y-%m-%d %H:%M} | {lead:,.1f} | {tops} |")
        lines.append("")
        if len(cm) > 1:   # accounts with several winning long shots across the case's markets
            multi = con.execute("""
                SELECT account_id, count(*) AS wins, sum(buy_notional) AS usd,
                       sum(buy_notional) / sum(buy_size) AS px
                FROM positions_all
                WHERE market_id IN (SELECT unnest(?)) AND is_winner AND buy_notional >= 250
                  AND buy_notional / buy_size < 0.35
                GROUP BY 1 HAVING count(*) >= 2 ORDER BY wins DESC, usd DESC LIMIT 6""",
                [list(cm.market_id)]).fetchall()
            if multi:
                lines += ["Accounts that won two or more long shots (bought below 0.35, ≥ $250) across these markets:", "",
                          "| Account | Long shots won | Cost | Avg price | Best rank |", "|---|---|---|---|---|"]
                for acct, wins, usd, px in multi:
                    r = ranked[(ranked.account_id == acct) & ranked.market_id.isin(cm.market_id)]["rank"]
                    lines.append(f"| {short_account(acct)} | {wins} | ${usd:,.0f} | {px:.2f} | "
                                 f"{int(r.min()) if len(r) else '—'} |")
                lines.append("")
    con.close()
    text = "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.md").write_text(text)
    summary["case_share_of_top25"] = int(n_case_top25)
    summary["case_positions"] = int(ranked.market_id.isin(cases.market_id).sum())
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
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
