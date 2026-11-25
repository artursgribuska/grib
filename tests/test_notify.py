"""Notifications: posted only when configured, wallets masked, failures never fatal."""
import httpx

from app import notify as n


def test_no_url_means_no_post(monkeypatch):
    monkeypatch.delenv("GRIB_ALERT_WEBHOOK_URL", raising=False)
    assert n.notify("hello") is False


def test_post_masks_wallets_and_truncates(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout, headers):
        sent.update(url=url, json=json)
        return httpx.Response(200)
    monkeypatch.setattr(n.httpx, "post", fake_post)
    text = "alert 0x" + "ab" * 20 + "\n" + ("line\n" * 2000)
    assert n.notify(text, url="https://hooks.example/x") is True
    body = sent["json"]["text"]
    assert ("ab" * 20) not in body and "[wallet]" in body and len(body) < 3_600 and "truncated" in body


def test_failures_are_not_fatal(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(n.httpx, "post", boom)
    assert n.notify("x", url="https://hooks.example/x") is False
    monkeypatch.setattr(n.httpx, "post", lambda *a, **k: httpx.Response(500, text="err"))
    assert n.notify("x", url="https://hooks.example/x") is False
