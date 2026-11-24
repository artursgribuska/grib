"""Wallet funding: scan windows, scanning with a fake RPC, links into scoring and bundles."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ingest import funding
from tests.test_features import T, build_db


def test_window_uses_join_date_and_caps_length():
    ft = datetime(2026, 11, 20)
    s, e = funding.window(ft, datetime(2026, 11, 18), "venue_join_date")
    assert s == datetime(2026, 11, 17) and e == ft
    s, e = funding.window(ft, datetime(2026, 9, 1), "venue_join_date")          # joined long before
    assert s == datetime(2026, 8, 31) and e - s == timedelta(days=funding.SCAN_DAYS)
    s, e = funding.window(ft, None, None)                                       # unknown join date
    assert e == ft and e - s == timedelta(days=3)


class FakeRpc:
    """Blocks are seconds since 2026-01-01; transfers keyed by wallet."""
    EPOCH = int(datetime(2026, 1, 1).timestamp())

    def __init__(self, transfers, archive=()):
        self.transfers, self.archive, self.calls = transfers, set(archive), 0

    def block_at(self, ts):
        return ts - self.EPOCH

    def block_ts(self, n):
        return n + self.EPOCH

    def transfers_in(self, wallet, b0, b1):
        if wallet in self.archive:
            raise funding.RpcError(-32602, "Archive requests require a personal token")
        return [t for t in self.transfers.get(wallet, []) if b0 <= t["block"] <= b1]

    def is_contract(self, addr):
        return addr == "bridge"


def _tx(sender, when, amount=1000.0):
    return {"sender": sender, "token": "USDC.e", "amount": amount, "tx": "0xt",
            "block": int(when.timestamp()) - FakeRpc.EPOCH}


@pytest.fixture(scope="module")
def funded():
    from scoring.run import run
    con = build_db()
    run(con)
    day = T - timedelta(hours=30)
    fake = FakeRpc({
        "insider": [_tx("boss", day)],
        "peer0": [_tx("boss", day)], "peer1": [_tx("boss", day)],
        "peer2": [_tx("bridge", day)], "peer3": [_tx("bridge", day)],
    }, archive={"flipper"})
    res = funding.run(con, fake, workers=1)
    scored = {(s.ctx.market_id, s.ctx.account_id): s for s in run(con)}
    return con, res, scored


def test_scan_records_status_and_transfers(funded):
    con, res, _ = funded
    st = dict(con.execute("SELECT account_id, status FROM wallet_funding_scans").fetchall())
    assert st["insider"] == "ok" and st["flipper"] == "archive_unavailable"
    assert con.execute("SELECT count(*) FROM wallet_funding_transfers WHERE account_id='insider'").fetchone()[0] == 1
    assert dict(con.execute("SELECT sender, is_contract FROM wallet_funding_senders").fetchall())["bridge"] is True


def test_shared_personal_funder_links_but_contract_does_not(funded):
    con, _, scored = funded
    edges = set(con.execute("SELECT a, b FROM funding_edges WHERE kind = 'shared_sender'").fetchall())
    assert ("insider", "peer0") in edges and ("peer2", "peer3") not in edges
    ins = scored[("slow", "insider")]
    assert ins.ctx.cohort_funding_links == 2
    ce = next(s for s in ins.signals if s.name == "coordinated_entry")
    assert ce.score == 1.0 and "same personal address" in ce.reason


def test_bundle_funding_section(funded):
    from cases.bundle import build_bundle
    con, _, _ = funded
    pid = con.execute("SELECT position_id FROM position_scores WHERE account_id='insider'").fetchone()[0]
    f = build_bundle(con, pid)["funding"]
    assert f["status"] == "ok"
    assert f["incoming_transfers"][0]["sender_type"] == "personal address"
    assert {w["display_id"] for w in f["wallets_sharing_a_personal_funder"]} == {"peer0", "peer1"}


def test_scoring_without_funding_data_still_works():
    from scoring.run import run
    con = build_db()
    s = {(x.ctx.market_id, x.ctx.account_id): x for x in run(con)}
    assert s[("slow", "insider")].ctx.funding_scan_status is None


def test_direct_link_only_between_trading_wallets():
    from scoring.run import run
    con = build_db()
    run(con)
    fake = FakeRpc({"insider": [_tx("peer0", T - timedelta(hours=30))]})   # a trading wallet funds another
    funding.run(con, fake, workers=1)
    s = {(x.ctx.market_id, x.ctx.account_id): x for x in run(con)}
    assert ("insider", "peer0") in set(con.execute("SELECT a, b FROM funding_edges WHERE kind='direct'").fetchall())
    assert s[("slow", "insider")].ctx.cohort_funding_links == 1
