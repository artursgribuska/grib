"""Known-cases study offline: fake Gamma event + trades, scored inside a small base universe."""
from datetime import timedelta

import duckdb

from eval import known_cases as kc
from tests.test_features import T, build_db

WALLET = "0x" + "ab" * 20


def _raw(wallet, hours_before, outcome, price, usd, side="BUY"):
    return {"proxy_wallet": wallet, "side": side, "outcome": outcome, "outcome_index": 0 if outcome == "Yes" else 1,
            "price": price, "size": usd / price, "timestamp": int((T - timedelta(hours=hours_before)).timestamp()),
            "token_id": "1", "transaction_hash": f"0x{hours_before}{wallet[-4:]}{outcome}{side}"}


def test_case_market_is_loaded_scored_and_reported(tmp_path, monkeypatch):
    base = tmp_path / "base.duckdb"
    src = build_db()
    src.execute(f"ATTACH '{base}' AS out")
    for t in ("markets", "trades", "accounts"):
        src.execute(f"CREATE TABLE out.{t} AS SELECT * FROM {t}")
    src.execute("DETACH out")

    event = {"id": "9", "title": "Prize 2026", "tags": [{"slug": "awards"}], "markets": [
        {"id": "1", "question": "Will Alice win the Prize in 2026?", "conditionId": "0xcase",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]', "clobTokenIds": '["1", "2"]',
         "startDate": (T - timedelta(days=40)).isoformat() + "Z", "endDate": T.isoformat() + "Z",
         "closedTime": T.isoformat() + "Z", "volumeNum": 1e5}]}
    raw = [_raw("0x" + f"{i:040x}", 24 * d, o, p, 800) for i, d in enumerate(range(3, 30))
           for o, p in (("Yes", 0.05), ("No", 0.95))]
    raw += [_raw(WALLET, 5, "Yes", 0.08, 4_000), _raw("0x" + "cd" * 20, 0.5, "Yes", 0.97, 500)]

    monkeypatch.setattr(kc, "DB", tmp_path / "kc.duckdb")
    monkeypatch.setattr(kc, "OUT", tmp_path / "kc")
    monkeypatch.setattr(kc, "CASES", [{"key": "prize", "events": ["prize-2026"], "match": ["Alice"], "about": "test"}])
    monkeypatch.setattr(kc, "gamma_event", lambda http, slug: event)
    monkeypatch.setattr(kc.DataApiClient, "iter_market_trades", lambda self, cid, max_rows=None: iter(raw))
    import ingest.enrich
    monkeypatch.setattr(ingest.enrich, "enrich", lambda con, wallets: 0)

    cases = kc.build(base_db=base)
    assert list(cases.case) == ["prize"] and cases.trades.iloc[0] == len(raw)
    con = duckdb.connect(str(tmp_path / "kc.duckdb"), read_only=True)
    ranked = [r[0] for r in con.execute("SELECT account_id FROM position_scores ORDER BY score DESC").fetchall()]
    assert WALLET in ranked[:2]          # the base universe has its own planted insider too
    con.close()
    text = kc.report()
    assert "## prize" in text and "best rank **" in text and "of the top 25 alerts" in text
    assert WALLET not in text                        # shown truncated only
    assert (tmp_path / "kc" / "report.md").exists()


def test_dashboard_shows_known_cases_summary(tmp_path, monkeypatch):
    import json
    import config
    from app.build import known_cases_html
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert known_cases_html() == ""                       # study not run: nothing shown
    (tmp_path / "known_cases").mkdir()
    (tmp_path / "known_cases" / "summary.json").write_text(json.dumps({
        "positions": 7350, "case_share_of_top25": 10, "case_positions": 384,
        "cases": [{"case": "nobel-2025", "markets": 1, "candidates": 92, "at_threshold": 3, "best_rank": 3,
                   "about": "<b>x</b>"}]}))
    html = known_cases_html()
    assert "nobel-2025" in html and "<b>3</b>" in html and "&lt;b&gt;x" in html
