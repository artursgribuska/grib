"""Follow up live-watch alerts: once their markets resolve, did the flagged side win?

The live watch raises alerts on open markets, before anyone knows the winner. That makes
every alert a prospective prediction that can be checked later. Winning on its own proves
nothing about any account. Across many alerts, though, the question is whether flagged
positions win more often than the price paid implied. A position bought at 0.10 is priced
to win about 10% of the time.

    uv run python -m watch.follow_up            # check resolutions now, print the scorecard

Each run looks up the markets of alerts whose outcome is not yet known (one Gamma request
per market), records outcomes in `watch_outcomes`, and writes data/live/scorecard.md.
`watch.run_live` runs it after every scan.
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime, timezone

import duckdb
import pandas as pd

import config
from ingest.http import CachedClient
from schema import polymarket as pm

DB = config.DATA_DIR / "live.duckdb"
OUT = config.DATA_DIR / "live"

OUTCOMES_DDL = """CREATE TABLE IF NOT EXISTS watch_outcomes (
    market_id VARCHAR PRIMARY KEY, checked_at TIMESTAMP, closed BOOLEAN,
    winning_outcome VARCHAR, resolved_at TIMESTAMP)"""


def lookup(http: CachedClient, market_id: str) -> dict | None:
    """Gamma's record for a market by condition id: closed markets need closed=true."""
    for closed in ("true", "false"):
        rows = http.get_json(f"{config.GAMMA_API}/markets", {"condition_ids": market_id, "closed": closed},
                             refresh=True)
        if rows:
            return rows[0]
    return None


def check(con, http: CachedClient | None = None, now: datetime | None = None, fetch=None) -> int:
    """Record outcomes for alerted markets not yet known to be resolved. Returns how many resolved now.
    `fetch(market_id) -> gamma market dict | None` may be injected (tests)."""
    con.execute(OUTCOMES_DDL)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if fetch is None:
        http = http or CachedClient()
        fetch = lambda mid: lookup(http, mid)
    todo = [r[0] for r in con.execute("""
        SELECT DISTINCT a.market_id FROM watch_alerts a LEFT JOIN watch_outcomes o USING (market_id)
        WHERE o.market_id IS NULL OR NOT o.closed OR o.winning_outcome IS NULL""").fetchall()]
    resolved = 0
    for mid in todo:
        m = fetch(mid)
        if m is None:
            continue
        closed = bool(m.get("closed"))
        winner = pm.winning_outcome(m) if closed else None
        ts = pm._parse_ts(m.get("closedTime")) if closed else None
        con.execute("INSERT OR REPLACE INTO watch_outcomes VALUES (?,?,?,?,?)", [mid, now, closed, winner, ts])
        resolved += winner is not None
    return resolved


def scorecard(con) -> pd.DataFrame:
    """Every alert with its result (won / lost / NULL while open). Works on a read-only connection."""
    has = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'watch_outcomes'").fetchone()[0]
    if not has:
        return con.execute("""
            SELECT kind, question, outcome, account_id, first_alerted_at, usd, avg_price,
                   NULL::VARCHAR AS winning_outcome, NULL::TIMESTAMP AS resolved_at, NULL::BOOLEAN AS won
            FROM watch_alerts ORDER BY first_alerted_at""").df()
    return con.execute("""
        SELECT a.kind, a.question, a.outcome, a.account_id, a.first_alerted_at, a.usd, a.avg_price,
               o.winning_outcome, o.resolved_at,
               CASE WHEN o.winning_outcome IS NULL THEN NULL ELSE a.outcome = o.winning_outcome END AS won
        FROM watch_alerts a LEFT JOIN watch_outcomes o USING (market_id)
        ORDER BY a.first_alerted_at""").df()


def excess_wins(s: pd.DataFrame) -> dict:
    """Wins among resolved alerts against the wins their prices implied. The z-score uses the
    Poisson-binomial variance (sum of p(1-p)); with few resolved alerts it means little."""
    r = s[s.won.notna()]
    n, wins = len(r), int(r.won.sum()) if len(r) else 0
    p = r.avg_price.clip(0.001, 0.999)
    expected, var = float(p.sum()), float((p * (1 - p)).sum())
    z = (wins - expected) / math.sqrt(var) if var > 0 else None
    return {"alerts": len(s), "resolved": n, "wins": wins, "expected_wins": expected, "z": z,
            "p_exact": exact_p(list(p), wins) if n else None}


def exact_p(probs: list[float], wins: int) -> float:
    """One-sided exact p-value: P(at least `wins` successes) under independent wins with these
    probabilities (Poisson-binomial). Preferred over the z approximation for small counts."""
    dist = [1.0]
    for q in probs:
        nxt = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nxt[k] += v * (1 - q)
            nxt[k + 1] += v * q
        dist = nxt
    return float(min(1.0, sum(dist[wins:])))


def report(con) -> str:
    from scoring.score import short_account
    s = scorecard(con)
    x = excess_wins(s)
    lines = ["# Live watch scorecard", "",
             f"{x['alerts']} alerts raised on open markets; {x['resolved']} of their markets have resolved.", ""]
    if x["resolved"]:
        z = f"z = {x['z']:+.1f}" if x["z"] is not None else "z not defined"
        lines += [f"Flagged side won **{x['wins']} of {x['resolved']}**; the prices paid implied "
                  f"**{x['expected_wins']:.1f}** ({z}). "
                  + ("Too few resolved alerts to draw a conclusion." if x["resolved"] < 20 else
                     "Positive z means flagged positions won more often than the market expected."), ""]
    lines += ["| Raised (UTC) | Alert | Market | Side | Account | Position | Avg price | Result |",
              "|---|---|---|---|---|---:|---:|---|"]
    for r in s.itertuples():
        res = "open" if pd.isna(r.won) else ("won" if r.won else f"lost ({r.winning_outcome})")
        lines.append(f"| {r.first_alerted_at:%Y-%m-%d %H:%M} | {r.kind.replace('_', ' ')} | {r.question} | "
                     f"{r.outcome} | {short_account(r.account_id)} | ${r.usd:,.0f} | {r.avg_price:.2f} | {res} |")
    lines += ["", "A win is not evidence against any account, and a loss does not clear one. Alerts are leads "
              "for review, not findings."]
    return "\n".join(lines) + "\n"


def run(db=None) -> str:
    con = duckdb.connect(str(db or DB))
    check(con)
    text = report(con)
    con.close()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "scorecard.md").write_text(text)
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args(argv)
    print(run(a.db))
    print(f"(written to {OUT / 'scorecard.md'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
