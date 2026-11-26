"""AI first look at each live alert: a short triage note drafted by Claude from the alert's facts.

A reviewer receiving a live alert needs three things fast: what happened, the strongest
innocent explanation, and what to do next. This drafts exactly that, from a fact sheet built
from our data only (the alert, the position, the venue's record of the account, the market's
state). Every number in the note must appear in the fact sheet and the language must be
neutral (the same checks as case files); a note that fails is retried once and kept flagged.
Notes are cached by a fingerprint of the facts, so unchanged alerts cost nothing.

    uv run python -m watch.triage            # notes for live alerts that have none (needs the API key)

It is a draft for a human reviewer, never a decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import datetime, timezone

import duckdb

import config
from cases.numbers import check_language, check_numbers
from cases.writer import EFFORT, MODEL, MissingApiKey, cost_usd, make_client
from scoring.score import short_account

log = logging.getLogger("triage")

DB = config.DATA_DIR / "live.duckdb"
MAX_TOKENS = 2000
ACTIONS = ["close", "monitor", "escalate"]

DDL = """CREATE TABLE IF NOT EXISTS watch_triage (
    kind VARCHAR, market_id VARCHAR, outcome VARCHAR, account_id VARCHAR, fingerprint VARCHAR, note_json VARCHAR,
    passed BOOLEAN, problems VARCHAR, model VARCHAR, cost_usd DOUBLE, created_at TIMESTAMP,
    PRIMARY KEY (kind, market_id, outcome, account_id))"""

SCHEMA = {
    "type": "object",
    "properties": {
        "what_happened": {"type": "string"},
        "innocent_explanation": {"type": "string"},
        "suggested_action": {"type": "string", "enum": ACTIONS},
        "action_reason": {"type": "string"},
    },
    "required": ["what_happened", "innocent_explanation", "suggested_action", "action_reason"],
    "additionalProperties": False,
}

SYSTEM = """You are a compliance analyst at a prediction-market venue doing a first look at a live alert,
before the market resolves. You receive a fact sheet. Write a short triage note:

- what_happened: two or three sentences on the activity behind the alert.
- innocent_explanation: the strongest ordinary reason for this activity (e.g. an active trader's
  usual style, public news, a reaction to a price move), and how well the facts fit it.
- suggested_action: "close" (the facts fit ordinary trading well), "monitor" (unclear; look again
  before resolution) or "escalate" (a review now, for example before payout, is warranted).
  Escalate mainly when a fresh or low-history account holds a position that is large for the market
  at long-shot prices. An account with a long, varied record usually points to an active trader.
- action_reason: one or two sentences.

Rules: use only facts in the sheet; every number you write must appear in the sheet (or write it in
words); refer to the account only by its display_id; neutral language: this is anomalous activity
warranting review, never a finding, and never words like insider, fraud, illegal, guilty or criminal.
The outcome of the market is not known."""


def fact_sheet(con, kind: str, market_id: str, outcome: str, account_id: str) -> dict:
    a = con.execute("""SELECT question, first_alerted_at, activity_at, usd, avg_price, detail FROM watch_alerts
                       WHERE kind = ? AND market_id = ? AND outcome = ? AND account_id = ?""",
                    [kind, market_id, outcome, account_id]).fetchone()
    q, raised, activity, usd, px, detail = a
    pos = con.execute("""SELECT count(*), coalesce(sum(notional), 0), min(ts), max(ts) FROM trades
                         WHERE market_id = ? AND account_id = ? AND outcome = ? AND side = 'buy'""",
                      [market_id, account_id, outcome]).fetchone()
    sold = con.execute("""SELECT coalesce(sum(notional), 0) FROM trades
                          WHERE market_id = ? AND account_id = ? AND outcome = ? AND side = 'sell'""",
                       [market_id, account_id, outcome]).fetchone()[0]
    mkt_buy = con.execute("SELECT coalesce(sum(notional), 0) FROM trades WHERE market_id = ? AND side = 'buy'",
                          [market_id]).fetchone()[0]
    from app.live_pages import outcome_prices
    px_now = outcome_prices(con, market_id, outcome)
    rec = con.execute("SELECT first_seen_at, metadata_json FROM accounts WHERE account_id = ?", [account_id]).fetchone()
    meta = json.loads(rec[1] or "{}") if rec else {}
    record = {}
    if rec and meta.get("first_seen_source") == "venue_join_date" and rec[0] is not None:
        record["joined_venue"] = f"{rec[0]:%Y-%m-%d}"
        record["account_age_days_at_first_buy"] = round(max((pos[2] - rec[0]).total_seconds(), 0) / 86400) if pos[2] else None
    for k, name in (("lifetime_markets_traded", "lifetime_markets_traded"), ("lifetime_volume_usd", "lifetime_volume_usd"),
                    ("lifetime_realized_pnl_usd", "lifetime_realized_profit_usd"),
                    ("lifetime_biggest_win_usd", "lifetime_biggest_win_usd")):
        if meta.get(k) is not None:
            record[name] = round(float(meta[k]))
    others = con.execute("""SELECT count(*) FROM watch_alerts WHERE market_id = ?
                            AND NOT (kind = ? AND outcome = ? AND account_id = ?)""",
                         [market_id, kind, outcome, account_id]).fetchone()[0]
    return {
        "alert": {"kind": kind.replace("_", " "), "raised_at": f"{raised:%Y-%m-%d %H:%M} UTC",
                  "activity_ended_at": f"{activity:%Y-%m-%d %H:%M} UTC", "detail": detail},
        "market": {"question": q, "outcome_bought": outcome, "status": "open (outcome not known)",
                   "current_price_of_outcome": round(float(px_now.p.iloc[-1]), 2) if len(px_now) else None,
                   "other_alerts_on_this_market": int(others)},
        "account": {"display_id": short_account(account_id), "venue_record": record or "not available"},
        "position_in_trades_held": {
            "buys": int(pos[0]), "bought_usd": round(float(pos[1])), "sold_usd": round(float(sold)),
            "average_price_at_alert": round(float(px), 2), "alert_position_usd": round(float(usd)),
            "share_of_all_buying_in_market_over_trades_held_pct":
                round(100 * float(pos[1]) / mkt_buy, 1) if mkt_buy else None,
            "first_buy": f"{pos[2]:%Y-%m-%d %H:%M} UTC" if pos[2] else None,
            "last_buy": f"{pos[3]:%Y-%m-%d %H:%M} UTC" if pos[3] else None},
        "data_notes": [
            "Trades held cover the last 7 days at the latest scan; older activity is not included.",
            "A percentage in the alert detail refers only to the buying during the price run-up or position-building "
            "window that triggered the alert; the share over all trades held is a different, broader measure.",
            "activity_ended_at is when the activity behind the alert ended; the account may have traded since, "
            "which the position figures include."],
    }


def note_text(n: dict) -> str:
    return "\n".join([n["what_happened"], n["innocent_explanation"], n["action_reason"]])


def _request(client, messages):
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM, messages=messages,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"no note: {resp.stop_reason}")
    return json.loads(next(b.text for b in resp.content if b.type == "text")), resp


def draft(client, facts: dict, max_attempts: int = 2) -> tuple[dict, list[str], str, float | None]:
    messages = [{"role": "user", "content": "Fact sheet:\n\n" + json.dumps(facts, indent=1, ensure_ascii=False)}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    for attempt in range(1, max_attempts + 1):
        note, resp = _request(client, messages)
        u = getattr(resp, "usage", None)
        if u is not None:
            usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        problems = [f"number not in the fact sheet: {x}" for x in check_numbers(note_text(note), facts)]
        problems += check_language(note_text(note))
        if not problems:
            break
        if attempt < max_attempts:
            messages = messages + [{"role": "assistant", "content": resp.content},
                                   {"role": "user", "content": "Fix these problems and return the full note: "
                                    + "; ".join(problems)}]
    return note, problems, getattr(resp, "model", MODEL), cost_usd(usage)


def fingerprint(facts: dict) -> str:
    stable = {k: v for k, v in facts.items() if k != "market"} | {"q": facts["market"]["question"]}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run(con, client=None) -> dict:
    """Draft notes for alerts without a current one. Skips (never fails) without an API key."""
    con.execute(DDL)
    alerts = con.execute("SELECT kind, market_id, outcome, account_id FROM watch_alerts").fetchall()
    todo = []
    for k, m, o, acc in alerts:
        f = fact_sheet(con, k, m, o, acc)
        fp = fingerprint(f)
        old = con.execute("SELECT fingerprint FROM watch_triage WHERE kind=? AND market_id=? AND outcome=? AND account_id=?",
                          [k, m, o, acc]).fetchone()
        if not old or old[0] != fp:
            todo.append((k, m, o, acc, f, fp))
    if not todo:
        return {"alerts": len(alerts), "drafted": 0}
    if client is None:
        try:
            client = make_client()
        except MissingApiKey as e:
            log.warning("triage notes skipped: %s", e)
            return {"alerts": len(alerts), "drafted": 0, "skipped": "no API key"}
    total = 0.0
    for k, m, o, acc, f, fp in todo:
        try:
            note, problems, model, cost = draft(client, f)
        except Exception as e:                   # one failed note must not stop the others
            log.warning("triage note failed for %s: %s", short_account(acc), str(e)[:120])
            continue
        total += cost or 0
        con.execute("INSERT OR REPLACE INTO watch_triage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [k, m, o, acc, fp, json.dumps(note), not problems, "; ".join(problems), model, cost,
                     datetime.now(timezone.utc).replace(tzinfo=None)])
    return {"alerts": len(alerts), "drafted": len(todo), "cost_usd": round(total, 4)}


def get_note(con, kind, market_id, outcome, account_id) -> dict | None:
    if "watch_triage" not in {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}:
        return None
    r = con.execute("""SELECT note_json, passed, problems, model FROM watch_triage
                       WHERE kind=? AND market_id=? AND outcome=? AND account_id=?""",
                    [kind, market_id, outcome, account_id]).fetchone()
    return None if not r else {**json.loads(r[0]), "passed": r[1], "problems": r[2], "model": r[3]}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db)
    print(json.dumps(run(con)))
    for k, m, o, acc in con.execute("SELECT kind, market_id, outcome, account_id FROM watch_alerts").fetchall():
        n = get_note(con, k, m, o, acc)
        if n:
            print(f"\n[{n['suggested_action'].upper()}] {short_account(acc)} · {'checks passed' if n['passed'] else n['problems']}")
            print(n["what_happened"], "\n", n["innocent_explanation"], "\n", n["action_reason"])
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
