"""Analyst review loop: decisions, audit trail, dashboard import, queue and stats."""
import json

import pytest

from app import review
from cases.bundle import case_id
from tests.test_features import build_db


@pytest.fixture()
def con():
    from scoring.run import run
    c = build_db()
    run(c)
    review.ensure(c)
    return c


def _top(con, n=1):
    return [r[0] for r in con.execute("SELECT position_id FROM position_scores ORDER BY score DESC LIMIT ?", [n]).fetchall()]


def test_decide_by_case_id_and_keep_history(con):
    pid = _top(con)[0]
    review.decide(con, case_id(pid), "needs_info", "asked for KYC", "ag")
    review.decide(con, case_id(pid), "dismissed", "public announcement before the buy", "ag")
    rows = con.execute("SELECT decision FROM reviews WHERE position_id = ? ORDER BY decided_at", [pid]).fetchall()
    assert [r[0] for r in rows] == ["needs_info", "dismissed"]
    review.ensure(con)
    assert con.execute("SELECT decision FROM review_status WHERE position_id = ?", [pid]).fetchone()[0] == "dismissed"


def test_dismissal_needs_a_note_and_decisions_are_validated(con):
    pid = _top(con)[0]
    with pytest.raises(ValueError):
        review.decide(con, pid, "dismissed", "")
    with pytest.raises(ValueError):
        review.decide(con, pid, "guilty", "x")
    with pytest.raises(KeyError):
        review.decide(con, "CASE-0000000000", "confirmed")


def test_queue_hides_closed_alerts(con):
    pid = _top(con)[0]
    before = set(review.queue(con).position_id)
    review.decide(con, pid, "confirmed", "", "ag")
    after = set(review.queue(con).position_id)
    assert pid in before and pid not in after


def test_import_from_dashboard_is_idempotent(con, tmp_path):
    pids = _top(con, 2)
    items = [{"case_id": case_id(pids[0]), "decision": "confirmed", "note": "", "reviewer": "ag",
              "decided_at": "2026-11-24T10:00:00.000Z"},
             {"case_id": case_id(pids[1]), "decision": "dismissed", "note": "", "reviewer": "ag",
              "decided_at": "2026-11-24T10:01:00.000Z"},          # no note -> rejected
             {"case_id": "CASE-FFFFFFFFFF", "decision": "confirmed"}]
    f = tmp_path / "d.json"
    f.write_text(json.dumps(items))
    n, problems = review.import_file(con, f)
    assert n == 1 and len(problems) == 2
    n2, _ = review.import_file(con, f)
    assert n2 == 0                                                     # same export imported twice
    assert con.execute("SELECT source FROM reviews").fetchone()[0] == "dashboard"


def test_stats_report_lift_and_flag_small_samples(con):
    pids = _top(con, 4)
    for i, pid in enumerate(pids):
        review.decide(con, pid, "confirmed" if i % 2 == 0 else "dismissed", "n", "ag")
    s = review.stats(con)
    assert s["closed_decisions"] == 4 and not s["enough"]
    assert {x["signal"] for x in s["signals"]} >= {"pre_resolution_timing", "coordinated_entry"}


def test_dashboard_shows_status_and_review_controls(con, tmp_path):
    from app.build import build
    pid = _top(con)[0]
    review.decide(con, pid, "escalated", "senior review", "ag")
    page = build(con, top=10, out=tmp_path / "i.html").read_text()
    assert 'data-status="escalated"' in page and "senior review" in page
    assert page.count('class="review"') >= 1 and "rv-export" in page


def test_review_cli_on_an_empty_database_says_what_to_do(tmp_path, monkeypatch, capsys):
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "empty.duckdb")
    assert review.main(["queue"]) == 1
    assert "run `uv run python -m pipeline` first" in capsys.readouterr().err
