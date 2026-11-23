"""A small, polite HTTP client: disk cache + per-host rate limit + retry with backoff.

Every GET response is cached on disk keyed by URL + parameters, so re-running the
pipeline does not hit the APIs again. Use `refresh=True` to bypass the cache for
data that can change (e.g. listing newly resolved markets).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

import config

log = logging.getLogger(__name__)


class RateLimiter:
    """Allows at most `rate` calls per second (shared across threads)."""

    def __init__(self, rate: float):
        self.min_interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class ApiError(RuntimeError):
    pass


class CachedClient:
    def __init__(self, cache_dir: Path = config.CACHE_DIR, offline: bool = False):
        self.cache_dir = cache_dir
        self.offline = offline  # True = never touch the network (tests, demos without wifi)
        self._client = httpx.Client(
            timeout=config.HTTP_TIMEOUT_S, headers={"User-Agent": config.USER_AGENT}
        )
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()
        self.stats = {"cache_hits": 0, "network_calls": 0, "retries": 0}

    # ---- cache -----------------------------------------------------------------
    @staticmethod
    def cache_key(url: str, params: dict | None) -> str:
        items = sorted((k, str(v)) for k, v in (params or {}).items() if v is not None)
        return hashlib.sha256(f"{url}?{urlencode(items)}".encode()).hexdigest()

    def _cache_path(self, url: str, params: dict | None) -> Path:
        host = urlparse(url).netloc
        key = self.cache_key(url, params)
        return self.cache_dir / host / key[:2] / f"{key}.json"

    # ---- request ---------------------------------------------------------------
    def _limiter(self, host: str) -> RateLimiter:
        with self._lock:
            if host not in self._limiters:
                self._limiters[host] = RateLimiter(config.REQUESTS_PER_SECOND.get(host, 2.0))
            return self._limiters[host]

    def get_json(self, url: str, params: dict | None = None, refresh: bool = False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        path = self._cache_path(url, params)
        if path.exists() and not refresh:
            self.stats["cache_hits"] += 1
            return json.loads(path.read_text())
        if self.offline:
            raise ApiError(f"offline mode and no cached response for {url} {params}")

        host = urlparse(url).netloc
        last_err: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            self._limiter(host).wait()
            try:
                self.stats["network_calls"] += 1
                resp = self._client.get(url, params=params)
            except httpx.TransportError as e:  # network blip: back off and retry
                last_err = e
                self._backoff(attempt, None)
                continue
            if resp.status_code == 200:
                payload = resp.json()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload))
                tmp.replace(path)
                return payload
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = ApiError(f"HTTP {resp.status_code} for {resp.url}: {resp.text[:200]}")
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            raise ApiError(f"HTTP {resp.status_code} for {resp.url}: {resp.text[:300]}")
        raise ApiError(f"giving up after {config.MAX_RETRIES} attempts: {last_err}")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        self.stats["retries"] += 1
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 2.0
        else:
            delay = min(60.0, 1.5 * 2**attempt) + random.uniform(0, 0.5)
        log.warning("backing off %.1fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)
