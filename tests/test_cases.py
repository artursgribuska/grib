import json
from types import SimpleNamespace

from cases.numbers import bundle_numbers, check_language, check_numbers, extract_numbers
from cases.writer import CASE_SCHEMA, render_markdown, write_case

BUNDLE = {
    "case_id": "CASE-0123456789",
    "disclaimer": "Demo.",
    "market": {"question": "Will X be named CEO?", "category": "personnel", "winning_outcome": "Yes",
               "outcome_became_public_at": "2026-11-23 13:59 UTC", "market_volume_usd": 46711.0},
    "account": {"display_id": "0xabab…abab"},
    "position": {"outcome_bought": "Yes", "cost_usd": 2891.0, "average_price": 0.27,
                 "payout_at_resolution_usd": 10734.0, "profit_usd": 7843.0},
    "score": {"total": 83.3, "points_by_signal": {"Pre-resolution timing": 24.4, "Category prior": 15.0}},
    "market_context": {"this_account_share_of_that_buying": 0.356},
    "signals": [{"reason": "Bought in the 72 h before the outcome became public."}],
    "data_limitations": ["No news feed."],
}


def test_extract_numbers_handles_money_percent_and_skips_hex_and_list_markers():
    toks = [t for t, *_ in extract_numbers("1. Paid $10,734 (35.6%) from 0x127b…e729 at 0.27\n2. Next")]
    assert toks == ["$10,734", "35.6%", "0.27"]


def test_number_check_accepts_rounding_and_percent_of_shares():
    ok = "Cost $2,891 at 0.27, paid $10,734; 36% of the buying (35.6%); score 83; 72 h; 13:59 on 2026-11-23."
    assert check_numbers(ok, BUNDLE) == []


def test_number_check_flags_invented_numbers():
    bad = "The account made a 271% return and has 12 linked wallets; profit was $7,900."
    assert set(check_numbers(bad, BUNDLE)) == {"271%", "12", "$7,900"}


def test_bundle_numbers_include_numbers_inside_strings():
    pool = bundle_numbers(BUNDLE)
    assert 72.0 in pool and 2026.0 in pool and 46711.0 in pool


def test_language_check():
    assert check_language("Anomalous activity warranting review by 0xabab…abab.") == []
    assert "banned term 'insider'" in check_language("Classic insider trading.")
    assert "full wallet address present" in check_language("wallet 0x" + "ab" * 20)


def _case(summary: str) -> dict:
    return {
        "summary": summary,
        "timeline": [{"time": "2026-11-23 13:59 UTC", "event": "Outcome became public."}],
        "evidence": [{"signal": "Pre-resolution timing", "finding": "Bought in the 72 h window.", "weight": "strong"}],
        "innocent_explanations": [{"explanation": "Public news", "fits": "cannot tell", "assessment": "No news feed."}],
        "risk_rating": {"level": "Medium", "confidence": "Medium", "rationale": "Score 83 but one wallet."},
        "next_steps": ["Request KYC."], "open_questions": ["Was there news?"],
    }


class FakeClient:
    """Stands in for anthropic.Anthropic(); returns scripted case files."""

    def __init__(self, cases):
        self.cases, self.calls = list(cases), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        text = json.dumps(self.cases.pop(0))
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                               content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=500))


def test_writer_retries_once_when_numbers_are_invented():
    client = FakeClient([_case("Paid $99,999 for 0.27."), _case("Paid $10,734 for a cost of $2,891.")])
    res = write_case(BUNDLE, client)
    assert res.attempts == 2 and res.checks_passed
    assert "$99,999" in client.calls[1]["messages"][-1]["content"]          # feedback names the bad number
    req = client.calls[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["output_config"]["format"]["schema"] == CASE_SCHEMA
    assert res.usage == {"input_tokens": 2000, "output_tokens": 1000}


def test_writer_flags_case_that_still_fails():
    client = FakeClient([_case("Insider made $99,999."), _case("Insider made $99,999.")])
    res = write_case(BUNDLE, client)
    assert not res.checks_passed and res.unmatched_numbers == ["$99,999"]
    md = render_markdown(BUNDLE, res)
    assert "NEEDS ATTENTION" in md and "untraced: $99,999" in md


def test_case_id_hides_wallet():
    from cases.bundle import case_id
    cid = case_id("0xabcdef1234:0x" + "ab" * 20)
    assert cid.startswith("CASE-") and "abab" not in cid.lower() and len(cid) == 15


def test_rendered_case_file_has_required_sections_and_no_full_wallet():
    res = write_case(BUNDLE, FakeClient([_case("Paid $10,734.")]))
    md = render_markdown(BUNDLE, res)
    for heading in ["## Summary", "## Timeline", "## Evidence", "## Innocent explanations considered",
                    "## Risk rating", "## Recommended next steps", "## Data limitations"]:
        assert heading in md
    assert "0x" + "ab" * 20 not in md
