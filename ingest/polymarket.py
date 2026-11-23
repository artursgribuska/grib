"""Thin clients for the two public Polymarket APIs we use.

Field names come from the official specs:
  * Data API v2:  https://data-api.polymarket.com/v2/openapi.json
  * Gamma API:    https://docs.polymarket.com/api-spec/gamma-openapi.yaml
Only the `/v2` Data API routes are used (v1 routes were retired on 2 Nov 2026).
"""

from __future__ import annotations

from collections.abc import Iterator

import config
from ingest.http import CachedClient


class GammaClient:
    """Market and event metadata (question text, tags, close time, final prices)."""

    def __init__(self, http: CachedClient):
        self.http = http

    def iter_closed_events(self, tag_slug: str, max_pages: int = 20, refresh: bool = False) -> Iterator[dict]:
        """Closed events for a tag, most recently closed first (keyset pagination)."""
        cursor = None
        for _ in range(max_pages):
            page = self.http.get_json(
                f"{config.GAMMA_API}/events/keyset",
                {
                    "closed": "true",
                    "tag_slug": tag_slug,
                    "order": "closedTime",
                    "ascending": "false",
                    "limit": 100,
                    "after_cursor": cursor,
                },
                refresh=refresh,
            )
            yield from page.get("events") or []
            cursor = page.get("next_cursor")
            if not cursor:
                return


class DataApiClient:
    """Trades, prices and wallet facts from the Data API v2 (`{data, pagination}` envelope)."""

    def __init__(self, http: CachedClient):
        self.http = http

    def iter_market_trades(self, condition_id: str, max_rows: int | None = None) -> Iterator[dict]:
        """Every fill in a market, both sides (taker_only=false), newest first.

        The trades feed is a keyset walk: we re-send identical filters on every page
        and follow `pagination.next_cursor` until it is null, as the spec requires.
        """
        cursor = None
        n = 0
        while True:
            page = self.http.get_json(
                f"{config.DATA_API}/trades",
                {"condition": condition_id, "taker_only": "false", "limit": 1000, "cursor": cursor},
            )
            rows = page.get("data") or []
            for r in rows:
                yield r
                n += 1
                if max_rows and n >= max_rows:
                    return
            pag = page.get("pagination") or {}
            cursor = pag.get("next_cursor")
            if not cursor or not pag.get("has_more", False):
                return

    def user_stats(self, wallet: str) -> dict | None:
        """Profile card: distinct markets traded, join date, PnL. `None` if unknown wallet."""
        return self.http.get_json(f"{config.DATA_API}/user-stats", {"user": wallet}).get("data")

    def price_history(self, token_id: str, interval: str = "max", bucket_seconds: int | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"token_id": token_id, "cursor": cursor}
            if cursor is None:
                params.update({"interval": interval, "bucket_seconds": bucket_seconds})
            page = self.http.get_json(f"{config.DATA_API}/prices-history", params)
            out.extend(page.get("data") or [])
            cursor = (page.get("pagination") or {}).get("next_cursor")
            if not cursor:
                return out
