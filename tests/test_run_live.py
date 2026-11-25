"""Live runner offline: fake Gamma listing and trades; alerts recorded once across runs."""
from datetime import datetime, timedelta

import duckdb

from watch import run_live

NOW = datetime(2026, 11, 25, 12, 0)


def _raw(wallet, hours_ago, price, usd, outcome="Yes"):
    return {"proxy_wallet": wallet, "side": "BUY", "outcome": outcome, "outcome_index": 0 if outcome == "Yes" else 1,
            "price": price, "size": usd / price, "token_id": "1",
            "timestamp": int((NOW - timedelta(hours=hours_ago)).timestamp()) - 0,
            "transaction_hash": f"0x{wallet[-4:]}{hours_ago}{outcome}"}


EVENT = {"id": "1", "title": "Who will be the next CEO?", "tags": [{"slug": "business"}], "markets": [
    {"id": "9", "question": "Will Alice be the next CEO of Acme?", "conditionId": "0xopen", "active": True,
     "closed": False, "outcomes": '["Yes", "No"]', "outcomePrices": '["0.1", "0.9"]', "clobTokenIds": '["1","2"]',
     "volume24hr": 50_000, "startDate": "2026-10-01T00:00:00Z", "endDate": "2026-12-31T00:00:00Z"}]}
TRADES = [_raw("0x" + "11" * 20, 30, 0.08, 6_000), _raw("0x" + "11" * 20, 20, 0.09, 6_000)] + \
         [_raw("0x" + f"{i:040x}", 40 + i, 0.9, 2_000, "No") for i in range(10)]


class FakeHttp:
    def get_json(self, url, params=None, refresh=False):
        return {"events": [EVENT]}


def test_live_run_records_new_alerts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(run_live, "DB", tmp_path / "live.duckdb")
    monkeypatch.setattr(run_live, "OUT", tmp_path / "live")
    monkeypatch.setattr(run_live, "CachedClient", lambda: FakeHttp())
    monkeypatch.setattr(run_live.DataApiClient, "iter_market_trades",
                        lambda self, cid, max_rows=None, refresh=False: iter(sorted(TRADES, key=lambda r: -r["timestamp"])))
    import ingest.enrich
    monkeypatch.setattr(ingest.enrich, "enrich", lambda con, wallets: 0)
    first = run_live.run(n_markets=10, now=NOW)
    assert first["markets"] == 1 and first["new"] == 1
    digest = (tmp_path / "live" / "watch.md").read_text()
    assert "long shot entry" in digest and "0x1111…1111" in digest and ("11" * 20) not in digest
    second = run_live.run(n_markets=10, now=NOW + timedelta(hours=3))
    assert second["new"] == 0
    con = duckdb.connect(str(tmp_path / "live.duckdb"), read_only=True)
    assert con.execute("SELECT count(*) FROM watch_alerts").fetchone()[0] == 1
