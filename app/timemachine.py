"""Before the News: an interactive time machine over the publicly reported cases.

Press play and history replays at speed: the winner's price, who was buying it (flagged
accounts against everyone else), each flagged account's first buy, and the moment the live
watch would have raised its alert, using only the trades available at that moment, with
a countdown to when the outcome actually became public. When the playhead reaches the
news, the page shows what the full engine concluded afterwards.

    uv run python -m app.timemachine        # → data/known_cases/timemachine.html (one self-contained file)

Built from data/known_cases.duckdb (eval/known_cases.py). Alerts are leads for review, not
findings; accounts are pseudonymous and shown truncated; the cases rest on public reports,
which are allegations.
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd

import config
from eval.known_cases import CASES
from eval.watch_replay import _replay_markets
from scoring.run import ROLLUP_MIN_SCORE
from scoring.score import LABELS, short_account
from watch.live import _account_facts

SHOWN = [
    ("maduro-2026", "Maduro, January 2026"),
    ("nobel-2025", "Nobel Peace Prize 2025"),
    ("us-strikes-iran-2026-02", "US strikes on Iran, February 2026"),
    ("iran-ceasefire-2026-04", "US–Iran ceasefire, April 2026"),
]
OUT_OF_SAMPLE = {"us-strikes-iran-2026-02", "iran-ceasefire-2026-04"}
MAX_DOTS = 160
TARGET_BUCKETS = 420


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def event_ranks(con) -> tuple[dict, int]:
    ev = con.execute("""
        WITH e AS (SELECT pe.event_key, max(ps.score) AS best FROM position_scores ps
                   JOIN position_events pe USING (position_id) GROUP BY 1),
             r AS (SELECT event_key, best, rank() OVER (ORDER BY best DESC) AS rank,
                          count(*) OVER (PARTITION BY best) > 1 AS tied FROM e)
        SELECT k."case", min(r.rank), (SELECT count(*) FROM e), bool_or(r.tied)
        FROM known_case_markets k JOIN position_scores ps USING (market_id)
        JOIN position_events pe USING (position_id) JOIN r USING (event_key) GROUP BY 1""").fetchall()
    return {c: (int(r), bool(tied)) for c, r, _, tied in ev}, int(ev[0][2]) if ev else 0


def pick_market(con, key: str) -> str:
    """The case market with the most flagged money (ties: highest score)."""
    return con.execute(f"""
        SELECT market_id FROM position_scores JOIN known_case_markets k USING (market_id)
        WHERE k."case" = ? AND score >= {ROLLUP_MIN_SCORE}
        GROUP BY 1 ORDER BY sum(position_notional) DESC, max(score) DESC LIMIT 1""", [key]).fetchone()[0]


LEAK_PRONE = ("personnel", "government_military", "corporate", "awards_entertainment")
UNRELATED = r"iran|maduro|venezuela|nobel|searched"     # the control must not touch any reported story


def control_candidates(con, n: int = 8) -> list[str]:
    """Ordinary markets for the control tab: leak-prone category, not a reported case, the winner's price
    rose sharply before the public moment, yet no position reached the flag line. Biggest rise first."""
    from scoring.market_screen import features
    f = features(con)
    calm = con.execute(f"""SELECT market_id FROM market_state WHERE market_id NOT IN (SELECT market_id FROM known_case_markets)
                           AND market_id NOT IN (SELECT market_id FROM position_scores WHERE score >= {ROLLUP_MIN_SCORE})""").df()
    f = f[f.market_id.isin(calm.market_id) & f.category.isin(LEAK_PRONE) & (f.rise >= 0.3)
          & ~f.question.str.contains(UNRELATED, case=False, regex=True)]
    return list(f.sort_values(["rise", "market_volume_pre"], ascending=False).market_id.head(n))


def market_event_rank(con, mid: str) -> tuple[int | None, bool]:
    r = con.execute("""
        WITH e AS (SELECT pe.event_key, max(ps.score) AS best FROM position_scores ps
                   JOIN position_events pe USING (position_id) GROUP BY 1),
             r AS (SELECT event_key, rank() OVER (ORDER BY best DESC) AS rank,
                          count(*) OVER (PARTITION BY best) > 1 AS tied FROM e)
        SELECT min(r.rank), bool_or(r.tied) FROM position_scores ps JOIN position_events pe USING (position_id)
        JOIN r USING (event_key) WHERE ps.market_id = ?""", [mid]).fetchone()
    return (int(r[0]), bool(r[1])) if r and r[0] is not None else (None, False)


def case_data(con, key: str, facts, seen, ranks: dict, n_events: int, mid: str | None = None,
              title: str | None = None, about: str | None = None) -> dict:
    mid = mid or pick_market(con, key)
    q, winner, public = con.execute(
        "SELECT question, winning_outcome, certainty_at FROM market_state WHERE market_id = ?", [mid]).fetchone()
    fl = con.execute(f"""
        SELECT ps.account_id, ps.score, ps.position_notional, ps.avg_price, ps.first_buy_ts, ps.contributions_json,
               ap.first_seen_at, ap.first_seen_source, coalesce(ap.lifetime_markets, ap.dataset_markets) AS markets
        FROM position_scores ps LEFT JOIN account_profile ap USING (account_id)
        WHERE ps.market_id = ? AND ps.score >= {ROLLUP_MIN_SCORE} ORDER BY ps.score DESC""", [mid]).df()
    alerts = _replay_markets(con, [mid], 2, facts, seen)
    if len(alerts):
        alerts = alerts[alerts["at"] < public].sort_values("at")

    firsts = list(fl.first_buy_ts) + (list(alerts["at"]) if len(alerts) else [])
    earliest = min(firsts) if firsts else public - timedelta(days=3)
    start = max(public - timedelta(days=14), earliest - timedelta(hours=36))
    end = public + timedelta(hours=6)
    span_min = (end - start).total_seconds() / 60
    bucket = next((b for b in (10, 15, 30, 60, 120, 180) if span_min / b <= TARGET_BUCKETS), 240)

    flagged_ids = list(fl.account_id)
    series = con.execute(f"""
        WITH b AS (
            SELECT time_bucket(INTERVAL {bucket} MINUTE, ts) AS t,
                   arg_max(winner_price, ts) FILTER (WHERE winner_price IS NOT NULL AND notional >= 5) AS p,
                   sum(notional) FILTER (WHERE side = 'buy' AND outcome = winning_outcome
                                         AND account_id IN (SELECT unnest(?))) AS flagged,
                   sum(notional) FILTER (WHERE side = 'buy' AND outcome = winning_outcome
                                         AND account_id NOT IN (SELECT unnest(?))) AS others
            FROM trades_w WHERE market_id = ? AND ts >= ? AND ts <= ? GROUP BY 1)
        SELECT * FROM b ORDER BY t""", [flagged_ids, flagged_ids, mid, start, end]).df()
    grid = pd.DataFrame({"t": pd.date_range(pd.Timestamp(start).floor(f"{bucket}min"), end, freq=f"{bucket}min")})
    series = grid.merge(series, on="t", how="left")
    series["p"] = series.p.ffill().bfill()
    series[["flagged", "others"]] = series[["flagged", "others"]].fillna(0)

    def age_days(r) -> float | None:
        if r.first_seen_source == "venue_join_date" and pd.notna(r.first_seen_at):
            return max((r.first_buy_ts - r.first_seen_at).total_seconds() / 86400, 0)
        return None

    def top_signals(js) -> list[str]:
        c = json.loads(js)
        return [LABELS.get(k, k) for k, v in sorted(c.items(), key=lambda kv: -kv[1]) if v > 0 and k in LABELS][:2]

    accounts = []
    for r in fl.itertuples():
        if not (start <= r.first_buy_ts <= end):
            continue
        a = age_days(r)
        accounts.append({"id": short_account(r.account_id), "t": _ms(r.first_buy_ts), "usd": round(float(r.position_notional)),
                         "px": round(float(r.avg_price), 3), "score": round(float(r.score), 1),
                         "age": None if a is None else round(a, 1),
                         "low": bool(pd.notna(r.markets) and r.markets <= 5), "why": top_signals(r.contributions_json)})
        if len(accounts) >= MAX_DOTS:
            break
    watch = [{"t": _ms(a.at), "kind": a.kind.replace("_", " "), "id": short_account(a.account_id), "usd": round(float(a.usd)),
              "px": round(float(a.avg_price), 3), "detail": a.detail, "lead": round(float(a.hours_before_public), 1),
              "winner": bool(a.on_winner)} for a in alerts.itertuples()] if len(alerts) else []
    about = about or next(c["about"] for c in CASES if c["key"] == key)
    first_win = next((w for w in watch if w["winner"]), None)
    top = fl.iloc[0] if len(fl) else None
    return {
        "key": key, "title": title or dict(SHOWN)[key], "control": key == "control", "question": q, "winner": winner, "about": about,
        "out_of_sample": key in OUT_OF_SAMPLE, "start": _ms(start), "end": _ms(end), "public": _ms(public),
        "bucket_min": bucket,
        "series": [[_ms(t), round(float(p), 4), round(float(f)), round(float(o))]
                   for t, p, f, o in zip(series.t, series.p, series.flagged, series.others)],
        "accounts": accounts, "watch": watch,
        "summary": {"event_rank": ranks.get(key, (None, False))[0], "tied": ranks.get(key, (None, False))[1],
                    "events": n_events, "flagged": int(len(fl)),
                    "flagged_usd": round(float(fl.position_notional.sum())) if len(fl) else 0,
                    "top_score": None if top is None else round(float(top.score), 1),
                    "top_id": None if top is None else short_account(top.account_id),
                    "first_alert_lead": None if first_win is None else first_win["lead"],
                    "cftc": key == "maduro-2026"},
    }


def build(con, out: Path | None = None) -> Path:
    out = out or config.DATA_DIR / "known_cases" / "timemachine.html"
    facts = _account_facts(con)
    seen = con.execute("SELECT account_id, count(DISTINCT market_id) n FROM trades GROUP BY 1").df().set_index("account_id").n
    ranks, n_events = event_ranks(con)
    present = {r[0] for r in con.execute(f"""SELECT DISTINCT k."case" FROM known_case_markets k
                                             JOIN position_scores ps USING (market_id)
                                             WHERE ps.score >= {ROLLUP_MIN_SCORE}""").fetchall()}
    data = [case_data(con, k, facts, seen, ranks, n_events) for k, _ in SHOWN if k in present]
    for mid in control_candidates(con):          # the control: first candidate where the watch stays quiet
        rank = market_event_rank(con, mid)
        c = case_data(con, "control", facts, seen, {"control": rank}, n_events, mid=mid, title="For comparison: an ordinary market",
                      about=CONTROL_ABOUT)
        if not c["watch"]:
            data.append(c)
            break
    html = TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out


CONTROL_ABOUT = ("Not the subject of any report: an ordinary market from the same universe, picked by rule (a leak-prone "
                 "category, no link to any reported story, the biggest price move towards the winner before the outcome became "
                 "public, and no position "
                 "the engine flagged). The price moves before the news here too, but no account's buying looks like the cases' "
                 "and the live watch stays quiet. Early price moves are common; what sets the cases apart is who was buying, "
                 "how early and how cheaply.")

TEMPLATE = (Path(__file__).parent / "timemachine.html").read_text()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(config.DATA_DIR / "known_cases.duckdb"))
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db, read_only=True)
    out = build(con)
    con.close()
    print(f"Written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
