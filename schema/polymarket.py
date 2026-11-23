"""Mapper: raw Polymarket rows -> canonical rows.

This is the ONLY place that knows Polymarket field names. If Polymarket changes a
field, fix it here; the signals do not change.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from schema.categories import categorize

VENUE = "polymarket"


def _parse_ts(value) -> datetime | None:
    """Gamma mixes ISO formats ('2026-11-23T09:51:34Z', '2026-11-23 22:29:51.06+00')."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    s = str(value).strip().replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if s.endswith("+00"):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def winning_outcome(market: dict) -> str | None:
    """The outcome label whose final price is exactly 1. None if not cleanly resolved
    (still open, voided, 50/50 split...)."""
    outcomes = _json_list(market.get("outcomes"))
    prices = [float(p) for p in _json_list(market.get("outcomePrices"))]
    if len(outcomes) < 2 or len(outcomes) != len(prices):
        return None
    winners = [o for o, p in zip(outcomes, prices) if p == 1.0]
    losers = [p for p in prices if p == 0.0]
    if len(winners) == 1 and len(losers) == len(prices) - 1:
        return winners[0]
    return None


def map_market(market: dict, event: dict) -> dict:
    tags = [t.get("slug") for t in (event.get("tags") or []) if t.get("slug")]
    question = market.get("question") or event.get("title") or ""
    meta = {
        "condition_id": market.get("conditionId"),
        "gamma_market_id": market.get("id"),
        "slug": market.get("slug"),
        "event_id": event.get("id"),
        "event_title": event.get("title"),
        "tags": tags,
        "outcomes": _json_list(market.get("outcomes")),
        "token_ids": _json_list(market.get("clobTokenIds")),
        "volume_usd": float(market.get("volumeNum") or market.get("volume") or 0),
        "neg_risk": bool(market.get("negRisk")),
        "resolution_source": market.get("resolutionSource"),
        "uma_resolution_status": market.get("umaResolutionStatus"),
    }
    return {
        # condition_id is the key the Data API trades feed uses
        "market_id": market.get("conditionId"),
        "venue": VENUE,
        "question": question,
        "category": categorize(question, tags),
        "opened_at": _parse_ts(market.get("startDate") or market.get("createdAt")),
        "closes_at": _parse_ts(market.get("endDate")),
        "resolved_at": _parse_ts(market.get("closedTime")),
        "winning_outcome": winning_outcome(market),
        "metadata_json": json.dumps(meta),
    }


def map_trades(raw_rows: list[dict], market_id: str) -> list[dict]:
    """Map Data API v2 trade rows (one market) to canonical trades.

    A row is one wallet's side of one fill. The API has no trade id, so we derive a
    stable one by hashing the row's content plus an occurrence counter (two genuinely
    identical fills in the same transaction stay distinct).
    Profile fields (display name, bio, images) are deliberately dropped: we analyse
    pseudonymous wallets and never link them to identities.
    """
    seen: Counter = Counter()
    out = []
    for r in raw_rows:
        price, size = r.get("price"), r.get("size")
        side = (r.get("side") or "").lower()
        wallet = (r.get("proxy_wallet") or "").lower()
        if price is None or size is None or side not in ("buy", "sell") or not wallet:
            continue
        basis = "|".join(str(x) for x in (
            r.get("transaction_hash"), wallet, r.get("token_id"), side, price, size, r.get("timestamp")))
        seen[basis] += 1
        trade_id = hashlib.sha1(f"{basis}|{seen[basis]}".encode()).hexdigest()[:24]
        out.append({
            "trade_id": trade_id,
            "venue": VENUE,
            "market_id": market_id,
            "account_id": wallet,
            "ts": _parse_ts(int(r["timestamp"])),
            "side": side,
            "outcome": r.get("outcome") or f"outcome_{r.get('outcome_index')}",
            "price": float(price),
            "size": float(size),
            "notional": float(price) * float(size),
        })
    return out
