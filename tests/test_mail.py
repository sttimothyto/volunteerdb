"""SMTP2GO send path via a mocked httpx transport — the 'never raises' contract."""

import json
from types import SimpleNamespace

import httpx

from volunteerdb.services import mail


def _fake_settings(monkeypatch) -> None:
    fake = SimpleNamespace(
        smtp2go_api_key="test-key",
        mail_from="vdb@example.org",
        mail_from_name="VolunteerDB",
    )
    monkeypatch.setattr(mail, "settings", lambda: fake)


def _mock_transport(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(mail.httpx, "AsyncClient", factory)


async def test_send_email_posts_to_smtp2go(monkeypatch):
    _fake_settings(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"succeeded": 1}})

    _mock_transport(monkeypatch, handler)

    assert await mail.send_email("to@example.org", "Hello", "Body text") is True
    (request,) = seen
    assert str(request.url) == mail.API_URL
    assert request.headers["X-Smtp2go-Api-Key"] == "test-key"
    payload = json.loads(request.content)
    assert payload["sender"] == "VolunteerDB <vdb@example.org>"
    assert payload["to"] == ["to@example.org"]
    assert payload["subject"] == "Hello"
    assert payload["text_body"] == "Body text"


async def test_send_email_api_error_returns_false(monkeypatch):
    _fake_settings(monkeypatch)

    _mock_transport(
        monkeypatch, lambda request: httpx.Response(500, json={"error": "boom"})
    )
    assert await mail.send_email("to@example.org", "s", "b") is False

    # a 200 that reports zero deliveries is also a failure
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": {"succeeded": 0}}),
    )
    assert await mail.send_email("to@example.org", "s", "b") is False


async def test_send_email_network_error_returns_false(monkeypatch):
    _fake_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_transport(monkeypatch, handler)
    assert await mail.send_email("to@example.org", "s", "b") is False


def test_ttl_window_renders_whole_days_as_days():
    assert mail.ttl_window(24) == "24 hours"
    assert mail.ttl_window(36) == "36 hours"
    assert mail.ttl_window(48) == "2 days"
    assert mail.ttl_window(168) == "7 days"


def test_invite_email_states_the_default_week():
    _, body = mail.invite_email("https://vdb.example.org/invite/tok")
    assert "7 days" in body, "the 168-hour default must read as days"
