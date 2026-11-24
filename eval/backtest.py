"""Backtest: plant synthetic trades in real markets and see how the engine ranks them.

    uv run python -m eval.backtest                 # 60 markets, seed 7
    uv run python -m eval.backtest --markets 100 --seed 3

There are no labelled insider cases in public data, so we measure the engine on
*planted* positions whose nature we know, inside an in-memory copy of the real data
(the database on disk is never modified):

  informed   a fresh wallet (joined the day before, 1 market traded) buys the eventual
             winner 2–36 h before it became public, at the price then prevailing (only
             where that price was below 0.60 — no edge otherwise), and holds.
  early      the same wallet profile and size, but bought 10–30 days before public:
             a long-held view, not timing.
  flipper    an informed-style buy that is sold back before the outcome became public.

For each kind we report how often it scores at or above the review threshold, and its
rank among all scored positions, overall and by category. A good engine ranks
`informed` high, and `early` / `flipper` well below it — and should not depend on the
market being slow-moving to do so (sports markets are reported separately).
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import timedelta

import duckdb
import pandas as pd

import config
from scoring.features import build_features
from scoring.run import run
from scoring.score import ALERT_THRESHOLD

SIZES_USD = (1_500, 5_000, 20_000)
MAX_INFORMED_PRICE = 0.60


def load_copy(db_path=config.DB_PATH) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"ATTACH '{db_path}' AS src (READ_ONLY)")
    for t in ("markets", "trades", "accounts"):
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t}")
    con.execute("DETACH src")
    return con


def _acct(kind: str, i: int) -> str:
    return "0x" + f"{kind[:4].encode().hex()}{i:06d}".ljust(40, "e")


def plant(con: duckdb.DuckDBPyConnection, n_markets: int, seed: int) -> pd.DataFrame:
    """Insert planted trades; return one row per planted position (market, account, kind)."""
    build_features(con)   # for certainty_at and the winner's price path
    rng = random.Random(seed)
    ms = con.execute("""SELECT market_id, venue, category, winning_outcome, certainty_at, first_trade_ts
                        FROM market_state ORDER BY market_id""").df()
    ms = ms.sample(n=min(n_markets, len(ms)), random_state=seed)
    trades, accounts, planted = [], [], []
    n = 0

    def price_at(mid, ts):
        r = con.execute("""SELECT winner_price FROM trades_w WHERE market_id = ? AND ts <= ?
                           ORDER BY ts DESC LIMIT 1""", [mid, ts]).fetchall()
        return float(r[0][0]) if r else None

    for i, m in enumerate(ms.itertuples()):
        cert = m.certainty_at.to_pydatetime()
        life_h = (cert - m.first_trade_ts.to_pydatetime()).total_seconds() / 3600
        usd = rng.choice(SIZES_USD)
        specs = [("informed", rng.uniform(2, 36), False), ("flipper", rng.uniform(4, 36), True)]
        if life_h > 24 * 12:
            specs.append(("early", rng.uniform(24 * 10, min(24 * 30, life_h - 1)), False))
        for kind, lead_h, flips in specs:
            lead_h = min(lead_h, max(life_h - 0.1, 0.2))
            ts = cert - timedelta(hours=lead_h)
            px = price_at(m.market_id, ts)
            if px is None or px >= MAX_INFORMED_PRICE or px <= 0.01:
                continue
            acct = _acct(kind, i)
            n += 1
            trades.append(dict(trade_id=f"eval{n}", venue=m.venue, market_id=m.market_id, account_id=acct, ts=ts,
                               side="buy", outcome=m.winning_outcome, price=px, size=usd / px, notional=usd))
            if flips:
                n += 1
                sell_ts = ts + timedelta(hours=min(2.0, lead_h / 2))
                spx = price_at(m.market_id, sell_ts) or px
                trades.append(dict(trade_id=f"eval{n}", venue=m.venue, market_id=m.market_id, account_id=acct,
                                   ts=sell_ts, side="sell", outcome=m.winning_outcome, price=spx,
                                   size=usd / px, notional=spx * usd / px))
            accounts.append(dict(account_id=acct, venue=m.venue, first_seen_at=ts - timedelta(days=1),
                                 metadata_json=json.dumps({"first_seen_source": "venue_join_date",
                                                           "lifetime_markets_traded": 1})))
            planted.append(dict(market_id=m.market_id, account_id=acct, kind=kind, category=m.category,
                                usd=usd, price=round(px, 3), lead_h=round(lead_h, 1)))
    for name, rows in (("trades", trades), ("accounts", accounts)):
        if rows:
            con.register("df", pd.DataFrame(rows))
            con.execute(f"INSERT INTO {name} SELECT * FROM df")
            con.unregister("df")
    return pd.DataFrame(planted)


def evaluate(con: duckdb.DuckDBPyConnection, planted: pd.DataFrame) -> pd.DataFrame:
    scored = run(con)
    n = len(scored)
    rank = {(s.ctx.market_id, s.ctx.account_id): (i, s.score) for i, s in enumerate(scored, 1)}
    out = planted.copy()
    out["rank"] = [rank.get((r.market_id, r.account_id), (None, None))[0] for r in out.itertuples()]
    out["score"] = [rank.get((r.market_id, r.account_id), (None, None))[1] for r in out.itertuples()]
    out["top_pct"] = out["rank"] / n
    out.attrs["n_scored"] = n
    return out


def summarise(res: pd.DataFrame) -> str:
    n = res.attrs.get("n_scored", 0)
    lines = [f"BACKTEST — {len(res)} planted positions among {n:,} scored "
             f"(review threshold {ALERT_THRESHOLD:.0f})", ""]
    res = res.assign(group=res.category.where(res.category.isin(["sports", "crypto_price"]), "other categories"))
    hdr = f"{'kind':<9} {'group':<17} {'n':>3} {'≥thr':>6} {'top 25':>7} {'top 1%':>7} {'med score':>9} {'med rank':>9}"
    lines.append(hdr)
    for (kind, grp), g in res.groupby(["kind", "group"]):
        s = g.dropna(subset=["score"])
        lines.append(f"{kind:<9} {grp:<17} {len(g):>3} {(s.score >= ALERT_THRESHOLD).mean():>6.0%} "
                     f"{(s['rank'] <= 25).mean():>7.0%} {(s.top_pct <= 0.01).mean():>7.0%} "
                     f"{s.score.median():>9.1f} {s['rank'].median():>9.0f}")
    lines += ["", "≥thr: share scoring at or above the review threshold. top 1%: share ranked in the top 1% of all "
              "positions. A planted position missing from the scores was not a candidate (e.g. sold out)."]
    return "\n".join(lines)


# Weights before signals 5 (price leadership) and 6 (track record) were added — the
# baseline `--compare` scores the same planted trades against.
BASELINE_WEIGHTS = {
    "pre_resolution_timing": 0.22, "low_probability_conviction": 0.22, "fresh_single_purpose_wallet": 0.18,
    "size_anomaly": 0.13, "category_prior": 0.12, "coordinated_entry": 0.13,
    "price_leadership": 0.0, "long_shot_track_record": 0.0,
}


def headline(res: pd.DataFrame) -> dict:
    """Informed vs controls in information-sensitive categories (the numbers that matter)."""
    r = res[~res.category.isin(["sports", "crypto_price"])].dropna(subset=["score"])
    out = {}
    for kind, g in r.groupby("kind"):
        out[kind] = {"n": len(g), "top1pct": (g.top_pct <= 0.01).mean(), "at_threshold": (g.score >= ALERT_THRESHOLD).mean(),
                     "median_score": g.score.median()}
    return out


def compare(con, planted) -> str:
    from scoring import score as sc
    cur = dict(sc.WEIGHTS)
    try:
        sc.WEIGHTS.clear(); sc.WEIGHTS.update(BASELINE_WEIGHTS)
        base = headline(evaluate(con, planted))
    finally:
        sc.WEIGHTS.clear(); sc.WEIGHTS.update(cur)
    now = headline(evaluate(con, planted))
    lines = [f"{'kind':<9} {'':>9} {'top 1%':>8} {'≥thr':>6} {'median':>7}"]
    for kind in sorted(now):
        for label, h in (("baseline", base.get(kind)), ("current", now[kind])):
            if h:
                lines.append(f"{kind:<9} {label:>9} {h['top1pct']:>8.0%} {h['at_threshold']:>6.0%} {h['median_score']:>7.1f}")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--compare", action="store_true", help="also score the same plants with the baseline weights")
    args = ap.parse_args(argv)
    con = load_copy()
    planted = plant(con, args.markets, args.seed)
    if args.compare:
        print(compare(con, planted))
        return
    res = evaluate(con, planted)
    out = config.OUTPUT_DIR / "backtest.csv"
    res.to_csv(out, index=False)
    print(summarise(res))
    print(f"\nPer-position results: {out}")


if __name__ == "__main__":
    main()
