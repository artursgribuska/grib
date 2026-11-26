"""AI first-look notes with a fake Claude: drafted, checked, retried on an invented number, cached."""
import json
import re
from types import SimpleNamespace

from tests.test_live_pages import ACCT, _live_db
from watch import triage


class FakeClaude:
    def __init__(self, notes):
        self.notes, self.calls = list(notes), 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        n = self.notes.pop(0) if len(self.notes) > 1 else self.notes[0]
        return SimpleNamespace(stop_reason="end_turn", model="fake", usage=None,
                               content=[SimpleNamespace(type="text", text=json.dumps(n))])


GOOD = {"what_happened": "The account bought 'Yes' three times at 0.2.", "innocent_explanation": "It may be reacting to news.",
        "suggested_action": "escalate", "action_reason": "A fresh account with a large long-shot position."}
BAD = {**GOOD, "what_happened": "The account bought 98765 USD of 'Yes'."}


def test_fact_sheet_has_no_full_wallet_and_key_facts():
    con = _live_db()
    f = triage.fact_sheet(con, "long_shot_entry", "m1", "Yes", ACCT)
    assert not re.search(r"0x[0-9a-fA-F]{40}", json.dumps(f))
    assert f["position_in_trades_held"]["bought_usd"] == 3000 and f["account"]["venue_record"]["lifetime_markets_traded"] == 2


def test_note_retried_on_invented_number_then_cached():
    con = _live_db()
    fake = FakeClaude([BAD, GOOD])
    res = triage.run(con, fake)
    assert res["drafted"] == 1 and fake.calls == 2
    n = triage.get_note(con, "long_shot_entry", "m1", "Yes", ACCT)
    assert n["passed"] and n["suggested_action"] == "escalate"
    assert triage.run(con, fake)["drafted"] == 0 and fake.calls == 2          # unchanged facts: no new call


def test_failed_checks_are_kept_flagged():
    con = _live_db()
    triage.run(con, FakeClaude([BAD]))
    n = triage.get_note(con, "long_shot_entry", "m1", "Yes", ACCT)
    assert not n["passed"] and "98765" in n["problems"]
