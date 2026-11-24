"""Milestone 5: load an operator's own bet / trade export (CSV) into the canonical tables.

    uv run python -m ingest.csv_loader --venue mybook --markets markets.csv --trades bets.csv [--accounts accounts.csv]
    GRIB_DB_PATH=data/mybook.duckdb uv run python -m pipeline --skip-ingest --no-funding

Use a separate database (GRIB_DB_PATH) so operator data never mixes with the public demo.
Examples of every file are in `examples/operator/`.

markets.csv (one row per market)
  market_id        required
  question         required  e.g. "Will X be appointed CEO by 31 March?" / "Arsenal v Spurs — match result"
  resolved_at      required  when the market settled (UTC, ISO 8601)
  winning_outcome  required  label exactly as used in bets.csv
  category         optional  one of schema/categories.py CATEGORIES; guessed from the question if missing
  opened_at        optional

bets.csv / trades.csv (one row per bet or fill, from the customer's side)
  market_id, account_id, ts (UTC), outcome   required
  then EITHER  price (implied probability 0..1) and size (shares / units)
  OR           decimal_odds (e.g. 4.50) and stake (money staked)
  side         optional, 'buy' (default) or 'sell' (cash-out / lay back)
  trade_id     optional, generated if missing

  Odds are converted as price = 1 / decimal_odds, size = stake × decimal_odds (the payout
  if it wins) and notional = stake. Bookmaker margin makes 1/odds slightly above the
  true probability; that errs on the side of fewer low-probability alerts.

accounts.csv (optional, one row per customer)
  account_id                         required
  joined_at                          optional  account opening date → wallet-age signal
  lifetime_markets_traded            optional  → single-purpose signal (else counted from the data)

Account IDs should be the operator's internal pseudonymous IDs, not names: case files
show them truncated, and nothing here needs personal data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

import config
from schema.canonical import create_tables
from schema.categories import CATEGORIES, categorize


@dataclass
class LoadReport:
    markets: int = 0
    trades: int = 0
    accounts: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)

    def reject(self, reason: str, n: int, example: str | None = None) -> None:
        if n:
            self.rejected[reason] = self.rejected.get(reason, 0) + int(n)
            if example and len(self.examples) < 10:
                self.examples.append(f"{reason}: {example}")

    def __str__(self) -> str:
        lines = [f"loaded {self.markets} markets, {self.trades:,} trades, {self.accounts} accounts"]
        for k, v in self.rejected.items():
            lines.append(f"  rejected {v:,} row(s): {k}")
        lines += [f"    e.g. {x}" for x in self.examples]
        return "\n".join(lines)


def _ts(s: pd.Series) -> pd.Series:
    # format="mixed": exports often mix "…T09:00:00" and "…T09:00:00.123456" or other ISO variants
    return pd.to_datetime(s, utc=True, errors="coerce", format="mixed").dt.tz_convert(None)


def _need(df: pd.DataFrame, cols: list[str], name: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: missing required column(s): {', '.join(missing)}")


def prepare_markets(df: pd.DataFrame, venue: str, rep: LoadReport) -> pd.DataFrame:
    df = df.rename(columns=str.strip)
    _need(df, ["market_id", "question", "resolved_at", "winning_outcome"], "markets")
    df = df.copy()
    df["market_id"] = df.market_id.astype(str).str.strip()
    df["resolved_at"] = _ts(df.resolved_at)
    df["opened_at"] = _ts(df.opened_at) if "opened_at" in df else pd.NaT
    bad = df.resolved_at.isna() | df.winning_outcome.isna() | (df.market_id == "")
    rep.reject("market without a valid resolved_at / winning_outcome", bad.sum(),
               ", ".join(df.loc[bad, "market_id"].head(3)))
    df = df[~bad]
    dup = df.market_id.duplicated()
    rep.reject("duplicate market_id", dup.sum(), ", ".join(df.loc[dup, "market_id"].head(3)))
    df = df[~dup]
    if "category" in df:
        given = df.category.fillna("").astype(str).str.strip()
        unknown = (given != "") & ~given.isin(CATEGORIES)
        rep.reject("unknown category (guessed from question instead)", unknown.sum(), ", ".join(given[unknown].head(3)))
        df["category"] = [g if g in CATEGORIES else categorize(q) for g, q in zip(given, df.question)]
    else:
        df["category"] = [categorize(q) for q in df.question]
    return pd.DataFrame({
        "market_id": df.market_id, "venue": venue, "question": df.question.astype(str),
        "category": df.category, "opened_at": df.opened_at, "closes_at": df.resolved_at,
        "resolved_at": df.resolved_at, "winning_outcome": df.winning_outcome.astype(str).str.strip(),
        "metadata_json": [json.dumps({"source": "operator_csv"})] * len(df)})


def prepare_trades(df: pd.DataFrame, markets: pd.DataFrame, venue: str, rep: LoadReport) -> pd.DataFrame:
    df = df.rename(columns=str.strip).copy()
    _need(df, ["market_id", "account_id", "ts", "outcome"], "trades")
    df["market_id"] = df.market_id.astype(str).str.strip()
    df["account_id"] = df.account_id.astype(str).str.strip()
    df["outcome"] = df.outcome.astype(str).str.strip()
    df["ts"] = _ts(df.ts)
    if {"price", "size"} <= set(df.columns):
        df["price"] = pd.to_numeric(df.price, errors="coerce")
        df["size"] = pd.to_numeric(df["size"], errors="coerce")
        df["notional"] = df.price * df["size"]
    elif {"decimal_odds", "stake"} <= set(df.columns):
        odds = pd.to_numeric(df.decimal_odds, errors="coerce")
        stake = pd.to_numeric(df.stake, errors="coerce")
        df["price"], df["size"], df["notional"] = 1 / odds, stake * odds, stake
    else:
        raise ValueError("trades: need either price + size, or decimal_odds + stake")
    df["side"] = df.side.astype(str).str.strip().str.lower() if "side" in df else "buy"

    checks = [
        ("unparseable timestamp", df.ts.isna()),
        ("price outside (0, 1) or odds ≤ 1", ~df.price.between(0, 1, inclusive="neither")),
        ("size / stake not positive", ~(df["size"] > 0)),
        ("side not buy/sell", ~df.side.isin(["buy", "sell"])),
        ("market not in markets file", ~df.market_id.isin(markets.market_id)),
        ("empty account_id", df.account_id.isin(["", "nan", "None"])),
    ]
    keep = pd.Series(True, index=df.index)
    for reason, bad in checks:
        bad = bad & keep
        rep.reject(reason, bad.sum(), df.loc[bad].head(1).to_dict("records").__str__()[:160] if bad.any() else None)
        keep &= ~bad
    df = df[keep]
    if "trade_id" in df:
        tid = df.trade_id.astype(str)
    else:
        basis = df.market_id + "|" + df.account_id + "|" + df.ts.astype(str) + "|" + df.outcome + "|" + \
            df.price.round(6).astype(str) + "|" + df["size"].round(6).astype(str)
        occ = basis.groupby(basis).cumcount().astype(str)
        tid = (basis + "|" + occ).map(lambda s: hashlib.sha1(s.encode()).hexdigest()[:24])
    out = pd.DataFrame({"trade_id": tid, "venue": venue, "market_id": df.market_id, "account_id": df.account_id,
                        "ts": df.ts, "side": df.side, "outcome": df.outcome, "price": df.price,
                        "size": df["size"], "notional": df.notional})
    dup = out.trade_id.duplicated()
    rep.reject("duplicate trade_id", dup.sum())
    return out[~dup]


def check_winners_traded(markets: pd.DataFrame, trades: pd.DataFrame, rep: LoadReport) -> None:
    traded = trades.groupby("market_id").outcome.agg(set)
    missing = [m for m, w in zip(markets.market_id, markets.winning_outcome) if w not in traded.get(m, set())]
    rep.reject("market whose winning_outcome never appears in trades (label mismatch?) — kept, but check",
               len(missing), ", ".join(missing[:3]))


def prepare_accounts(df: pd.DataFrame | None, trades: pd.DataFrame, venue: str, rep: LoadReport) -> pd.DataFrame:
    first = trades.groupby("account_id").ts.min()
    acc = pd.DataFrame({"account_id": first.index, "first_trade": first.values})
    if df is not None:
        df = df.rename(columns=str.strip).copy()
        _need(df, ["account_id"], "accounts")
        df["account_id"] = df.account_id.astype(str).str.strip()
        df = df.drop_duplicates("account_id")
        acc = acc.merge(df, on="account_id", how="left")
    joined = _ts(acc.joined_at) if "joined_at" in acc else pd.Series(pd.NaT, index=acc.index)
    lifetime = pd.to_numeric(acc.lifetime_markets_traded, errors="coerce") if "lifetime_markets_traded" in acc \
        else pd.Series(float("nan"), index=acc.index)
    meta = []
    for j, lm in zip(joined, lifetime):
        m = {"first_seen_source": "venue_join_date" if pd.notna(j) else "dataset_first_trade"}
        if pd.notna(lm):
            m["lifetime_markets_traded"] = int(lm)
        meta.append(json.dumps(m))
    return pd.DataFrame({"account_id": acc.account_id, "venue": venue,
                         "first_seen_at": joined.where(joined.notna(), acc.first_trade), "metadata_json": meta})


def load(con: duckdb.DuckDBPyConnection, venue: str, markets_csv: Path, trades_csv: Path,
         accounts_csv: Path | None = None) -> LoadReport:
    rep = LoadReport()
    create_tables(con)
    m = prepare_markets(pd.read_csv(markets_csv), venue, rep)
    t = prepare_trades(pd.read_csv(trades_csv), m, venue, rep)
    check_winners_traded(m, t, rep)
    a = prepare_accounts(pd.read_csv(accounts_csv) if accounts_csv else None, t, venue, rep)
    for table, df, key in (("markets", m, "market_id"), ("trades", t, "trade_id"), ("accounts", a, "account_id")):
        con.register("_df", df)
        con.execute(f"DELETE FROM {table} WHERE venue = ? AND {key} IN (SELECT {key} FROM _df)", [venue])
        con.execute(f"INSERT INTO {table} SELECT * FROM _df")
        con.unregister("_df")
    rep.markets, rep.trades, rep.accounts = len(m), len(t), len(a)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", required=True, help="short name for this data source, e.g. 'mybook'")
    ap.add_argument("--markets", required=True, type=Path)
    ap.add_argument("--trades", required=True, type=Path)
    ap.add_argument("--accounts", type=Path)
    a = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH))
    try:
        rep = load(con, a.venue, a.markets, a.trades, a.accounts)
    except ValueError as e:
        print(f"!! {e}", file=sys.stderr)
        return 2
    finally:
        con.close()
    print(f"{rep}\ninto {config.DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
