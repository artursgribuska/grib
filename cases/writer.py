"""LLM case-file writer.

bundle (JSON) --> Claude (structured JSON output) --> checks --> Markdown case file

Checks after every draft:
  * number check   — every number must trace to the bundle (cases/numbers.py)
  * language check — no accusatory terms, no full wallet addresses
If a draft fails, the model gets one chance to fix the listed problems. A draft that
still fails is saved but flagged `checks_passed = False` for human attention.

Model and key: `claude-sonnet-5-5`, API key from GRIB_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)
in the environment or a local `.env` file. Never hard-code keys.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from cases.numbers import check_language, check_numbers

MODEL = "claude-sonnet-5-5"
# list prices, USD per million tokens (input, output), for the cost line in run output; update if prices change
PRICE_PER_MTOK = {"claude-sonnet-5-5": (2.0, 10.0)}


def cost_usd(usage: dict, model: str = MODEL) -> float | None:
    price = PRICE_PER_MTOK.get(model)
    if not price or not usage:
        return None
    return (usage.get("input_tokens", 0) * price[0] + usage.get("output_tokens", 0) * price[1]) / 1e6
MAX_TOKENS = 16_000
EFFORT = "high"
PROMPT_PATH = Path(__file__).parent / "prompts" / "case_file_system.md"

_RISK = {"type": "string", "enum": ["Low", "Medium", "High"]}
CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "timeline": {"type": "array", "items": {
            "type": "object",
            "properties": {"time": {"type": "string"}, "event": {"type": "string"}},
            "required": ["time", "event"], "additionalProperties": False}},
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {"signal": {"type": "string"}, "finding": {"type": "string"},
                           "weight": {"type": "string", "enum": ["strong", "moderate", "weak"]}},
            "required": ["signal", "finding", "weight"], "additionalProperties": False}},
        "innocent_explanations": {"type": "array", "items": {
            "type": "object",
            "properties": {"explanation": {"type": "string"},
                           "fits": {"type": "string", "enum": ["fits", "partly fits", "does not fit", "cannot tell"]},
                           "assessment": {"type": "string"}},
            "required": ["explanation", "fits", "assessment"], "additionalProperties": False}},
        "risk_rating": {"type": "object",
                        "properties": {"level": _RISK, "confidence": _RISK, "rationale": {"type": "string"}},
                        "required": ["level", "confidence", "rationale"], "additionalProperties": False},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "timeline", "evidence", "innocent_explanations", "risk_rating",
                 "next_steps", "open_questions"],
    "additionalProperties": False,
}


class MissingApiKey(RuntimeError):
    pass


@dataclass
class CaseResult:
    case: dict
    unmatched_numbers: list[str]
    language_problems: list[str]
    attempts: int
    model: str
    usage: dict = field(default_factory=dict)

    @property
    def checks_passed(self) -> bool:
        return not self.unmatched_numbers and not self.language_problems


def make_client():
    """Anthropic client. Raises MissingApiKey with a plain-language hint if no key is set.

    Reads GRIB_ANTHROPIC_API_KEY first: Claude Code cloud sessions strip ANTHROPIC_API_KEY
    from the container, so the key has to be stored under another name there.
    """
    load_dotenv()
    key = os.environ.get("GRIB_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise MissingApiKey(
            "No API key found. Set GRIB_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) in your "
            "environment or in a .env file in the project folder, then run again.")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def llm_text(case: dict) -> str:
    """All free text the model wrote (what the checks run on)."""
    parts = [case["summary"], case["risk_rating"]["rationale"]]
    parts += [f"{t['time']} {t['event']}" for t in case["timeline"]]
    parts += [e["finding"] for e in case["evidence"]]
    parts += [f"{i['explanation']} {i['assessment']}" for i in case["innocent_explanations"]]
    parts += case["next_steps"] + case["open_questions"]
    return "\n".join(parts)


def _request(client, messages: list) -> tuple[dict, object]:
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": PROMPT_PATH.read_text(), "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": CASE_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",   # if a safety classifier declines, retry on a suitable fallback model
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model declined to write this case file: {getattr(resp, 'stop_details', None)}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("case file was cut off (max_tokens); raise MAX_TOKENS")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), resp


def write_case(bundle: dict, client=None, max_attempts: int = 2) -> CaseResult:
    client = client or make_client()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Evidence bundle:\n\n" + json.dumps(bundle, indent=1, ensure_ascii=False)},
        {"type": "text", "text": "Write the case file for this alert."},
    ]}]
    usage: dict = {"input_tokens": 0, "output_tokens": 0}
    for attempt in range(1, max_attempts + 1):
        case, resp = _request(client, messages)
        u = getattr(resp, "usage", None)
        if u is not None:
            usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        text = llm_text(case)
        unmatched = check_numbers(text, bundle)
        lang = check_language(text)
        if not unmatched and not lang:
            break
        if attempt < max_attempts:
            problems = []
            if unmatched:
                problems.append("These numbers do not appear in the evidence bundle: " + ", ".join(sorted(set(unmatched)))
                                + ". Remove them or replace them with numbers that are in the bundle, or use words.")
            if lang:
                problems.append("Language problems: " + "; ".join(lang) + ". Use neutral wording and the display_id only.")
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": " ".join(problems) + " Return the full corrected case file."},
            ]
    return CaseResult(case, unmatched, lang, attempt, getattr(resp, "model", MODEL), usage)


# --------------------------------------------------------------------------- rendering
def render_markdown(bundle: dict, result: CaseResult) -> str:
    c, m, a, p, s = result.case, bundle["market"], bundle["account"], bundle["position"], bundle["score"]
    rr = c["risk_rating"]
    lines = [
        f"# Case file: {m['question']}",
        "",
        f"> {bundle['disclaimer']}",
        "",
        "| | |", "|---|---|",
        f"| Case ID | `{bundle['case_id']}` |",
        f"| Account | `{a['display_id']}` (pseudonymous) |",
        f"| Market | {m['question']} ({m['category'].replace('_', ' ')}) |",
        f"| Outcome bought / winner | {p['outcome_bought']} / {m['winning_outcome']} |",
        f"| Position | ${p['cost_usd']:,.0f} at avg {p['average_price']:.2f} → paid ${p['payout_at_resolution_usd']:,.0f} |",
        f"| Engine score | **{s['total']:.0f} / 100** |",
        f"| Risk rating | **{rr['level']}** (confidence: {rr['confidence']}) |",
        f"| Automated checks | {'passed' if result.checks_passed else 'NEEDS ATTENTION — see end of file'} |",
        "",
        "## Summary", "", c["summary"], "",
        "## Timeline", "",
        "| Time | Event |", "|---|---|",
        *[f"| {t['time']} | {t['event']} |" for t in c["timeline"]],
        "",
        "## Evidence", "",
        *[f"- **{e['signal']}** ({e['weight']}): {e['finding']}" for e in c["evidence"]],
        "",
        "### Score breakdown (engine)", "",
        "| Signal | Points |", "|---|---|",
        *[f"| {k} | {v:+.1f} |" for k, v in s["points_by_signal"].items()],
        f"| **Total** | **{s['total']:.1f}** |",
        "",
        "## Innocent explanations considered", "",
        *[f"- **{i['explanation']}** — *{i['fits']}*. {i['assessment']}" for i in c["innocent_explanations"]],
        "",
        "## Risk rating", "", f"**{rr['level']}** (confidence: {rr['confidence']}). {rr['rationale']}", "",
        "## Recommended next steps", "",
        *[f"{n}. {step}" for n, step in enumerate(c["next_steps"], 1)],
        "",
        "## Open questions", "",
        *[f"- {q}" for q in c["open_questions"]],
        "",
        "## Data limitations", "",
        *[f"- {d}" for d in bundle["data_limitations"]],
        "",
        "---",
        f"*Drafted by {result.model} from the evidence bundle only; requires review by a compliance analyst. "
        f"Number check: {'all numbers traced to the bundle' if not result.unmatched_numbers else 'untraced: ' + ', '.join(result.unmatched_numbers)}. "
        f"Language check: {'passed' if not result.language_problems else '; '.join(result.language_problems)}.*",
    ]
    return "\n".join(lines) + "\n"
