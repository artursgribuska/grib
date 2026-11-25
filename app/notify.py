"""Push a short message to a team channel when there is something new to review.

Set GRIB_ALERT_WEBHOOK_URL to an incoming-webhook URL (Slack, Mattermost, Discord via
its /slack endpoint, or anything that accepts {"text": ...}). Unset = no notifications.
Messages carry the same content as the digests: truncated accounts, no full wallets.
A failed post is logged and never stops a run.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

import config

log = logging.getLogger("notify")
MAX_CHARS = 3_500           # stay well inside chat message limits
_FULL_WALLET = re.compile(r"0x[0-9a-fA-F]{40}")


def notify(text: str, url: str | None = None) -> bool:
    url = url or os.environ.get("GRIB_ALERT_WEBHOOK_URL")
    if not url or not text.strip():
        return False
    text = _FULL_WALLET.sub("[wallet]", text)          # belt and braces: digests are already truncated
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n… (truncated — see the dashboard)"
    try:
        r = httpx.post(url, json={"text": text}, timeout=config.HTTP_TIMEOUT_S,
                       headers={"User-Agent": config.USER_AGENT})
        if r.status_code >= 300:
            log.warning("notification failed: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("notification failed: %s", e)
        return False
