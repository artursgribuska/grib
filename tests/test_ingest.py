import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import config
from ingest import run
from ingest.http import ApiError, CachedClient, RateLimiter


class FakeTransport(httpx.BaseTransport):
    """Replays a scripted list of responses and counts calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        status, body, headers = self.responses.pop(0)
        return httpx.Response(status, json=body, headers=headers)


def _client(tmp_path, responses, monkeypatch):
    c = CachedClient(cache_dir=tmp_path)
    t = FakeTransport(responses)
    c._client = httpx.Client(transport=t)
    monkeypatch.setattr(c, "_backoff", lambda attempt, ra: None)  # no real sleeping in tests
    return c, t


def test_cache_hit_avoids_second_call(tmp_path, monkeypatch):
    c, t = _client(tmp_path, [(200, {"data": [1]}, {})], monkeypatch)
    assert c.get_json("https://x.test/a", {"q": 1}) == {"data": [1]}
    assert c.get_json("https://x.test/a", {"q": 1}) == {"data": [1]}
    assert t.calls == 1 and c.stats["cache_hits"] == 1


def test_retries_on_429_then_succeeds(tmp_path, monkeypatch):
    c, t = _client(tmp_path, [(429, {}, {"Retry-After": "0"}), (503, {}, {}), (200, {"ok": True}, {})], monkeypatch)
    assert c.get_json("https://x.test/b") == {"ok": True}
    assert t.calls == 3


def test_client_error_is_not_retried(tmp_path, monkeypatch):
    c, t = _client(tmp_path, [(400, {"error": "bad"}, {})], monkeypatch)
    with pytest.raises(ApiError):
        c.get_json("https://x.test/c")
    assert t.calls == 1


def test_offline_mode_never_calls_network(tmp_path):
    with pytest.raises(ApiError):
        CachedClient(cache_dir=tmp_path, offline=True).get_json("https://x.test/d")


def test_rate_limiter_spaces_calls():
    rl = RateLimiter(rate=20)  # 50 ms apart
    t0 = time.monotonic()
    for _ in range(5):
        rl.wait()
    assert time.monotonic() - t0 >= 0.19


def _mk(cid, vol=10_000, prices='["1", "0"]', closed_days_ago=3, outcomes='["Yes", "No"]', q="Will X happen?"):
    closed = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=closed_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"conditionId": cid, "question": q, "closed": True, "outcomes": outcomes,
            "outcomePrices": prices, "closedTime": closed, "volumeNum": vol}


def test_eligibility_rules():
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    assert run._eligible(_mk("a"), cutoff)
    assert not run._eligible(_mk("b", vol=10), cutoff)                       # too small
    assert not run._eligible(_mk("c", prices='["0.5", "0.5"]'), cutoff)      # no clean winner
    assert not run._eligible(_mk("d", closed_days_ago=90), cutoff)           # too old
    assert not run._eligible(_mk("e", outcomes='["A","B","C"]', prices='["1","0","0"]'), cutoff)


def test_sample_respects_event_cap_and_quota(monkeypatch):
    monkeypatch.setitem(config.CATEGORY_QUOTAS, "sports", 2)
    sports_ev = {"id": "s", "tags": [{"slug": "sports"}]}
    other_evs = [{"id": f"e{i}", "tags": [{"slug": "geopolitics"}]} for i in range(3)]
    cands = [(_mk(f"s{i}", q="Lakers vs. Celtics"), sports_ev) for i in range(10)]
    cands += [(_mk(f"g{i}{j}", q="Will a military strike happen?"), other_evs[i]) for i in range(3) for j in range(10)]
    chosen = run.choose_sample(cands, 50)
    ids = [m["conditionId"] for m, _ in chosen]
    assert sum(i.startswith("s") for i in ids) == 2     # sports quota
    assert len(ids) == 2 + 3 * config.SELECTION["max_markets_per_event"]  # per-event cap
    assert len(set(ids)) == len(ids)
