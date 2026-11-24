"""Wallet funding: who sent USDC to the flagged wallets when they started trading?

Polymarket accounts are proxy wallets on Polygon, funded with USDC. Two wallets funded
from the same personal address (or one funding the other) are very likely controlled
together — the strongest evidence of coordination available in public data.

    uv run python -m ingest.funding            # flagged wallets + their entry-cohort peers
    uv run python -m ingest.funding --offline  # cache only (tests / no network)

What is scanned, per wallet: USDC.e, native USDC and pUSD (Polymarket USD) `Transfer` logs *into* the wallet
in a window around when it started trading — from a day before its venue join date (or
its first trade in our data, if the join date is unknown) up to that first trade,
at most SCAN_DAYS long. That is where the initial deposit sits.

Limits, stated plainly:
  * Public RPC nodes serve only recent history without a key. Older windows are
    recorded as `archive_unavailable` (set POLYGON_RPC_URL to an archive node to fix).
  * Only USDC transfers on Polygon are seen: deposits through bridges, exchanges or
    card on-ramps arrive from shared infrastructure addresses and link nothing.
    Scoring treats contract senders and senders that fund wallets across many markets
    as infrastructure, not as links.

Tables (Polymarket/Polygon-specific, optional — scoring works without them):
  wallet_funding_scans      one row per wallet: window, status, transfers found
  wallet_funding_transfers  one row per incoming transfer: sender, token, amount, time
  wallet_funding_senders    one row per sender: is it a contract?
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import httpx
import pandas as pd

import config
from ingest.http import RateLimiter

log = logging.getLogger("funding")

RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
RPC_RATE = 5.0                 # requests per second
MAX_BLOCK_SPAN = 10_000        # per eth_getLogs call (public node limit)
SECONDS_PER_BLOCK = 2.0        # first guess for block <-> time; refined against real headers
FINALITY_BLOCKS = 512          # only cache log ranges this far behind the head
SCAN_DAYS = 7
PRE_JOIN_HOURS = 24
END_MARGIN = timedelta(minutes=15)   # deposits often land seconds before the first trade; block lookup rounds down
TOKENS = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD",   # Polymarket USD, the venue's collateral token
}
ZERO_ADDRESS = "0x" + "0" * 40   # token mints (e.g. pUSD minted on deposit) — no personal sender
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CACHE = config.CACHE_DIR / "polygon-rpc"
MIN_TARGET_SCORE = 40.0        # wallets in alerts at/above this score, plus their cohort peers


class RpcError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message

    @property
    def archive(self) -> bool:
        m = self.message.lower()
        return "archive" in m or "missing trie node" in m or "pruned" in m


class Rpc:
    """Minimal JSON-RPC client: rate-limited, retried, disk-cached for immutable answers."""

    def __init__(self, url: str = RPC_URL, offline: bool = False, cache: Path = CACHE):
        self.url, self.offline, self.cache = url, offline, cache
        self._http = httpx.Client(timeout=config.HTTP_TIMEOUT_S, headers={"User-Agent": config.USER_AGENT})
        self._limit = RateLimiter(RPC_RATE)
        self.calls = 0
        self._head: int | None = None

    def _path(self, method, params) -> Path:
        key = hashlib.sha256(json.dumps([self.url, method, params], sort_keys=True).encode()).hexdigest()
        return self.cache / key[:2] / f"{key}.json"

    def call(self, method: str, params: list, cacheable: bool = True):
        path = self._path(method, params)
        if cacheable and path.exists():
            return json.loads(path.read_text())
        if self.offline:
            raise RpcError("offline", f"no cached answer for {method}")
        last = None
        for attempt in range(config.MAX_RETRIES):
            self._limit.wait()
            self.calls += 1
            try:
                r = self._http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except httpx.TransportError as e:
                last = e
                time.sleep(min(30, 1.5 * 2 ** attempt))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = RpcError(r.status_code, r.text[:200])
                time.sleep(min(30, 1.5 * 2 ** attempt))
                continue
            body = r.json()
            if "error" in body:
                err = RpcError(body["error"].get("code"), body["error"].get("message", ""))
                if err.archive or err.code == -32701:
                    raise err
                last = err
                time.sleep(min(30, 1.5 * 2 ** attempt))
                continue
            res = body["result"]
            if cacheable:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(res))
            return res
        raise RpcError("retries", str(last))

    def head(self) -> int:
        if self._head is None:
            self._head = int(self.call("eth_blockNumber", [], cacheable=False), 16)
        return self._head

    def block_ts(self, n: int) -> int:
        return int(self.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)

    def block_at(self, ts: int) -> int:
        """Last block at or before unix time `ts` (secant search on real block headers)."""
        head = self.head()
        hi_n, hi_t = head, self.block_ts(head)
        if ts >= hi_t:
            return head
        n = max(0, head - int((hi_t - ts) / SECONDS_PER_BLOCK))
        for _ in range(8):
            t = self.block_ts(n)
            step = int((ts - t) / SECONDS_PER_BLOCK)
            if abs(step) <= 30:
                break
            n = max(0, min(head, n + step))
        while n > 0 and self.block_ts(n) > ts:   # settle exactly on the right side
            n -= 30
        return n

    def is_contract(self, addr: str) -> bool:
        return self.call("eth_getCode", [addr, "latest"]) not in ("0x", "0x0", None)

    def transfers_in(self, wallet: str, start: int, end: int) -> list[dict]:
        topic_to = "0x" + "0" * 24 + wallet.lower()[2:]
        out = []
        for b in range(start, end + 1, MAX_BLOCK_SPAN):
            e = min(b + MAX_BLOCK_SPAN - 1, end)
            logs = self.call("eth_getLogs", [{"address": list(TOKENS), "topics": [TRANSFER_TOPIC, None, topic_to],
                                              "fromBlock": hex(b), "toBlock": hex(e)}],
                             cacheable=e < self.head() - FINALITY_BLOCKS)
            for lg in logs:
                out.append({"sender": "0x" + lg["topics"][1][26:], "token": TOKENS.get(lg["address"].lower(), lg["address"]),
                            "amount": int(lg["data"], 16) / 1e6, "block": int(lg["blockNumber"], 16),
                            "tx": lg["transactionHash"]})
        return out


DDL = """
CREATE TABLE IF NOT EXISTS wallet_funding_scans (
    account_id VARCHAR PRIMARY KEY, from_ts TIMESTAMP, to_ts TIMESTAMP, from_block BIGINT, to_block BIGINT,
    status VARCHAR, n_transfers INTEGER, scanned_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS wallet_funding_transfers (
    account_id VARCHAR, sender VARCHAR, token VARCHAR, amount DOUBLE, block BIGINT, ts TIMESTAMP, tx VARCHAR);
CREATE TABLE IF NOT EXISTS wallet_funding_senders (sender VARCHAR PRIMARY KEY, is_contract BOOLEAN);
"""


def targets(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Wallets in alerts (score ≥ MIN_TARGET_SCORE) plus their entry-cohort peers, with scan windows."""
    return con.execute(f"""
        WITH flagged AS (SELECT DISTINCT ps.account_id FROM position_scores ps WHERE ps.score >= {MIN_TARGET_SCORE}),
             peers AS (SELECT DISTINCT unnest(ec.winner_peer_ids) AS account_id
                       FROM entry_cohorts ec JOIN position_scores ps USING (market_id, account_id)
                       WHERE ps.score >= {MIN_TARGET_SCORE}),
             w AS (SELECT account_id FROM flagged UNION SELECT account_id FROM peers)
        SELECT w.account_id, min(t.ts) AS first_trade, any_value(a.first_seen_at) AS first_seen_at,
               any_value(json_extract_string(a.metadata_json, '$.first_seen_source')) AS src
        FROM w JOIN trades t USING (account_id) LEFT JOIN accounts a USING (account_id)
        GROUP BY 1 ORDER BY 1""").df()


def window(first_trade: datetime, first_seen: datetime | None, src: str | None) -> tuple[datetime, datetime]:
    anchor = first_seen if (src == "venue_join_date" and first_seen is not None and first_seen < first_trade) else first_trade
    start = anchor - timedelta(hours=PRE_JOIN_HOURS)
    end = min(first_trade, start + timedelta(days=SCAN_DAYS))
    if end - start < timedelta(days=3):          # unknown join date: look back a few days before first trade
        start = end - timedelta(days=3)
    return start, end + END_MARGIN


def scan_one(rpc: Rpc, row) -> tuple[dict, list[dict]]:
    ft = row.first_trade.to_pydatetime()
    fs = row.first_seen_at.to_pydatetime() if pd.notna(row.first_seen_at) else None
    start, end = window(ft, fs, row.src)
    scan = {"account_id": row.account_id, "from_ts": start, "to_ts": end, "from_block": None, "to_block": None,
            "status": "ok", "n_transfers": 0, "scanned_at": datetime.now(timezone.utc).replace(tzinfo=None)}
    try:
        b0, b1 = rpc.block_at(int(start.replace(tzinfo=timezone.utc).timestamp())), \
                 rpc.block_at(int(end.replace(tzinfo=timezone.utc).timestamp()))
        scan.update(from_block=b0, to_block=b1)
        found = rpc.transfers_in(row.account_id, b0, b1)
    except RpcError as e:
        scan["status"] = "archive_unavailable" if e.archive else f"error: {str(e)[:80]}"
        return scan, []
    tx_rows = []
    for f in found:
        ts = datetime.fromtimestamp(rpc.block_ts(f["block"]), timezone.utc).replace(tzinfo=None)
        tx_rows.append({"account_id": row.account_id, **f, "ts": ts})
    scan["n_transfers"] = len(tx_rows)
    return scan, tx_rows


def run(con: duckdb.DuckDBPyConnection, rpc: Rpc, workers: int = 4, rescan: bool = False) -> dict:
    con.execute(DDL)
    tg = targets(con)
    # archive_unavailable will fail again on the same node: retried only with --rescan (e.g. after
    # pointing POLYGON_RPC_URL at an archive node)
    done = set() if rescan else {r[0] for r in con.execute(
        "SELECT account_id FROM wallet_funding_scans WHERE status IN ('ok', 'archive_unavailable')").fetchall()}
    todo = [r for r in tg.itertuples() if r.account_id not in done]
    log.info("%d target wallets (%d already scanned), scanning %d", len(tg), len(done), len(todo))
    scans, transfers = [], []
    with ThreadPoolExecutor(workers) as ex:
        for i, (s, t) in enumerate(ex.map(lambda r: scan_one(rpc, r), todo), 1):
            scans.append(s)
            transfers += t
            if i % 25 == 0:
                log.info("  %d/%d wallets scanned (%d RPC calls)", i, len(todo), rpc.calls)
    if scans:
        ids = [s["account_id"] for s in scans]
        con.execute("DELETE FROM wallet_funding_scans WHERE account_id IN (SELECT unnest(?))", [ids])
        con.execute("DELETE FROM wallet_funding_transfers WHERE account_id IN (SELECT unnest(?))", [ids])
        _insert(con, "wallet_funding_scans", pd.DataFrame(scans))
        if transfers:
            _insert(con, "wallet_funding_transfers",
                    pd.DataFrame(transfers)[["account_id", "sender", "token", "amount", "block", "ts", "tx"]])
    senders = [r[0] for r in con.execute("""SELECT DISTINCT sender FROM wallet_funding_transfers
                                            WHERE sender NOT IN (SELECT sender FROM wallet_funding_senders)""").fetchall()]
    rows = []
    for s in senders:
        if s == ZERO_ADDRESS:
            rows.append({"sender": s, "is_contract": True})   # a mint: infrastructure by definition
            continue
        try:
            rows.append({"sender": s, "is_contract": rpc.is_contract(s)})
        except RpcError:
            pass
    if rows:
        _insert(con, "wallet_funding_senders", pd.DataFrame(rows))
    status = dict(con.execute("SELECT status, count(*) FROM wallet_funding_scans GROUP BY 1").fetchall())
    return {"targets": len(tg), "scanned_now": len(todo), "rpc_calls": rpc.calls, "status": status,
            "transfers": con.execute("SELECT count(*) FROM wallet_funding_transfers").fetchone()[0]}


def _insert(con, table: str, df: pd.DataFrame) -> None:
    con.register("_df", df)
    con.execute(f"INSERT INTO {table} SELECT * FROM _df")
    con.unregister("_df")


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--rescan", action="store_true", help="scan wallets again even if already scanned")
    args = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH))
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if not {"position_scores", "entry_cohorts"} <= tables:
        raise SystemExit("Run `uv run python -m scoring.run` first: funding targets come from the alerts.")
    res = run(con, Rpc(offline=args.offline), rescan=args.rescan)
    log.info("done: %s", res)
    con.close()


if __name__ == "__main__":
    main()
