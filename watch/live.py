"""Live watch: alerts on OPEN markets, before anyone knows the winner.

The main engine reviews resolved markets (it needs the winner). A compliance team gets
more value from an alert *before payout*, while withdrawals can still be held. Two
patterns from the publicly reported cases are visible while a market is still open:

  long_shot_entry   a fresh or low-history account builds a heavy long-shot position: its
                    cumulative buying of one outcome (avg price ≤ 0.20, ≥ $2,500) reaches 5%
                    of the market's buying in the previous 7 days, or $10,000. Cumulative, so
                    drip-buying in small chunks does not slip under the bar. Maduro, January
                    2026: an account that joined 5 days earlier bought 'Yes' at 7–10¢ in chunks
                    of $85–7,215 over three days.
  run_up_initiator  an outcome's price jumps by ≥ 0.20 from ≤ 0.50 within 2 hours, and one
                    account did ≥ 40% (and ≥ $1,000) of the buying that took it halfway up.
                    Nobel Peace Prize 2025: one account did most of the buying that took the
                    winner from 3¢ to 75¢, ten hours before the announcement.

`scan(con, as_of)` uses only trades at or before `as_of`, so the same code runs live
(as_of = now, on markets still open) and as a replay of history (`replay`), which is how
lead time and alert volume are measured. Alerts are leads for review, not findings.

    uv run python -m watch.live --replay            # replay the known cases + alert volume
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

import config

# long-shot entry
ENTRY_MAX_PRICE = 0.20
ENTRY_MIN_USD = 2_500
ENTRY_MIN_SHARE = 0.05          # of the market's buying (all outcomes) in the trailing window …
ENTRY_ABS_USD = 10_000          # … or this much in total, whichever comes first
ENTRY_WINDOW_H = 24 * 7
FRESH_MAX_DAYS = 14             # "fresh": joined ≤ this long before the buy (venue join date) …
LOW_HISTORY_MAX_MARKETS = 5     # … or has traded ≤ this many markets
# run-up initiator
RUN_WINDOW_H = 2
RUN_MAX_START = 0.50
RUN_MIN_RISE = 0.20
INIT_MIN_SHARE = 0.40
INIT_MIN_USD = 1_000
LOOKBACK_H = 6                  # each scan looks at activity in the last few hours (scans run at least this often)


@dataclass
class WatchAlert:
    kind: str
    market_id: str
    question: str
    outcome: str
    account_id: str
    at: datetime                # when the alert could first have fired
    usd: float
    avg_price: float
    detail: str


def _account_facts(con) -> pd.DataFrame:
    return con.execute("""
        SELECT a.account_id, a.first_seen_at,
               json_extract_string(a.metadata_json, '$.first_seen_source') AS src,
               TRY_CAST(json_extract(a.metadata_json, '$.lifetime_markets_traded') AS INTEGER) AS lifetime
        FROM accounts a""").df().set_index("account_id")


def _is_low_history(facts: pd.DataFrame, acct: str, at: datetime, markets_seen: int) -> tuple[bool, str]:
    """Fresh (joined ≤ FRESH_MAX_DAYS before `at`) or low-history (≤ LOW_HISTORY_MAX_MARKETS lifetime).
    The venue's lifetime count, when known, is decisive; markets seen in our own data are only a
    fallback for accounts the venue knows nothing about (with a small sample, almost every account
    has been 'seen in few markets')."""
    if acct in facts.index:
        f = facts.loc[acct]
        if f.src == "venue_join_date" and pd.notna(f.first_seen_at):
            age = (at - f.first_seen_at.to_pydatetime()).total_seconds() / 86400
            if age <= FRESH_MAX_DAYS:
                return True, f"account {max(age, 0):.0f} days old"
        if pd.notna(f.lifetime):
            if f.lifetime <= LOW_HISTORY_MAX_MARKETS:
                return True, f"{int(f.lifetime)} markets traded in its lifetime"
            return False, ""
    if markets_seen <= LOW_HISTORY_MAX_MARKETS:
        return True, f"{markets_seen} market(s) seen in our data (venue history unknown)"
    return False, ""


def load_trades(con: duckdb.DuckDBPyConnection, market_ids: list[str] | None = None) -> pd.DataFrame:
    mfilter = "WHERE t.market_id IN (SELECT unnest(?))" if market_ids else ""
    return con.execute(f"""
        SELECT t.market_id, m.question, m.resolved_at, t.account_id, t.ts, t.side, t.outcome, t.price, t.notional
        FROM trades t JOIN markets m USING (venue, market_id) {mfilter}
        ORDER BY t.ts""", [market_ids] if market_ids else []).df()


def scan(con: duckdb.DuckDBPyConnection, as_of: datetime, market_ids: list[str] | None = None,
         lookback_h: float = LOOKBACK_H, trades: pd.DataFrame | None = None,
         facts: pd.DataFrame | None = None, markets_seen: pd.Series | None = None) -> list[WatchAlert]:
    """Alerts that fire on activity in (as_of − lookback_h, as_of], using only trades up to as_of.
    Markets already resolved at as_of are skipped (nothing left to protect).
    `trades` / `facts` / `markets_seen` may be passed in preloaded (replays)."""
    since = as_of - timedelta(hours=lookback_h)
    t = trades if trades is not None else load_trades(con, market_ids)
    t = t[(t.ts <= as_of) & (t.resolved_at.isna() | (t.resolved_at > as_of))]
    if t.empty:
        return []
    facts = facts if facts is not None else _account_facts(con)
    seen = markets_seen if markets_seen is not None else t.groupby("account_id").market_id.nunique()
    alerts: list[WatchAlert] = []
    recent = t[t.ts > since]
    for mid, g in recent.groupby("market_id"):
        full = t[t.market_id == mid]
        q = g.question.iloc[0]
        # ---- long-shot entry: per account and outcome, CUMULATIVE buys so far (fires when the
        # latest buy is inside the lookback window, i.e. when the position grew)
        win = full[(full.side == "buy") & (full.ts > as_of - timedelta(hours=ENTRY_WINDOW_H))]
        market_buy = win.notional.sum()
        active = set(map(tuple, g[g.side == "buy"][["account_id", "outcome"]].drop_duplicates().values))
        allbuys = full[full.side == "buy"]
        for (acct, out), b in allbuys.groupby(["account_id", "outcome"]):
            if (acct, out) not in active:
                continue
            usd = b.notional.sum()
            if usd < ENTRY_MIN_USD:
                continue
            px = usd / (b.notional / b.price).sum()
            share = usd / market_buy if market_buy > 0 else 0.0
            if px > ENTRY_MAX_PRICE or (share < ENTRY_MIN_SHARE and usd < ENTRY_ABS_USD):
                continue
            low, why = _is_low_history(facts, acct, b.ts.min().to_pydatetime(), int(seen.get(acct, 1)))
            if not low:
                continue
            alerts.append(WatchAlert("long_shot_entry", mid, q, out, acct, b.ts.max().to_pydatetime(), usd, px,
                                     f"{why}; ${usd:,.0f} so far in {len(b)} buy(s), {share:.0%} of the market's "
                                     f"buying in the last 7 days"))
        # ---- run-up initiator: per outcome, hourly last price; a run-up ending inside the window
        for out, o in full.groupby("outcome"):
            p = o.set_index("ts").price.sort_index()
            hourly = p.resample("1h").last().ffill()
            if len(hourly) <= RUN_WINDOW_H:
                continue
            start = hourly.shift(RUN_WINDOW_H)
            peak = hourly.rolling(RUN_WINDOW_H).max()
            rise = (peak - start).where(start <= RUN_MAX_START)
            rise = rise[rise.index > since - timedelta(hours=1)]
            if rise.dropna().empty or rise.max() < RUN_MIN_RISE:
                continue
            end_h = rise.idxmax()
            t0 = end_h - timedelta(hours=RUN_WINDOW_H - 1)
            half_level = start[end_h] + rise[end_h] / 2
            in_move = o[(o.ts >= t0) & (o.ts < end_h + timedelta(hours=1))]
            reached = in_move[in_move.price >= half_level]
            t_half = reached.ts.min() if len(reached) else in_move.ts.max()
            ib = o[(o.side == "buy") & (o.ts >= t0) & (o.ts <= t_half)]
            if ib.empty:
                continue
            shares = ib.groupby("account_id").notional.sum()
            for acct, usd in shares.items():
                share = usd / shares.sum()
                if share >= INIT_MIN_SHARE and usd >= INIT_MIN_USD:
                    mine = ib[ib.account_id == acct]
                    px = usd / (mine.notional / mine.price).sum()
                    alerts.append(WatchAlert("run_up_initiator", mid, q, out, acct, t_half.to_pydatetime(), usd, px,
                                             f"{share:.0%} of the buying that moved '{out}' from {start[end_h]:.2f} "
                                             f"towards {peak[end_h]:.2f}"))
    return alerts


def replay(con, start: datetime, end: datetime, step_h: float = LOOKBACK_H,
           market_ids: list[str] | None = None, trades: pd.DataFrame | None = None,
           facts: pd.DataFrame | None = None, markets_seen: pd.Series | None = None) -> pd.DataFrame:
    """Run scan() every step_h hours over [start, end]; first firing per (kind, market, outcome, account)."""
    trades = trades if trades is not None else load_trades(con, market_ids)
    facts = facts if facts is not None else _account_facts(con)
    out: dict[tuple, WatchAlert] = {}
    t = start
    while t <= end:
        for a in scan(con, t, lookback_h=step_h, trades=trades, facts=facts, markets_seen=markets_seen):
            out.setdefault((a.kind, a.market_id, a.outcome, a.account_id), a)
        t += timedelta(hours=step_h)
    cols = list(WatchAlert.__dataclass_fields__)
    return pd.DataFrame([asdict(a) for a in out.values()], columns=cols)


def case_lead_times(con, alerts: pd.DataFrame) -> pd.DataFrame:
    """For alerts on the winning side of markets that later resolved: hours before public certainty."""
    if alerts.empty:
        return alerts
    st = con.execute("SELECT market_id, winning_outcome, certainty_at FROM market_state").df()
    a = alerts.merge(st, on="market_id", how="left")
    a["on_winner"] = a.outcome == a.winning_outcome
    a["hours_before_public"] = (a.certainty_at - a["at"]).dt.total_seconds() / 3600
    return a


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", action="store_true", help="replay the known-cases database and report")
    ap.add_argument("--db", default=str(config.DATA_DIR / "known_cases.duckdb"))
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db, read_only=True)
    if a.replay:
        from eval.watch_replay import report
        text = report(con)
        out = config.DATA_DIR / "known_cases" / "watch_replay.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(text)
        print(f"(written to {out})")
    else:
        alerts = scan(con, datetime.now(timezone.utc).replace(tzinfo=None))
        for x in alerts:
            print(json.dumps(asdict(x), default=str))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
