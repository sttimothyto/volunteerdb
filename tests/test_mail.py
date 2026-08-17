"""SMTP2GO send path via a mocked httpx transport — the 'never raises' contract."""

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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


def test_event_digest_email_sections_and_link():
    tz = ZoneInfo("America/Toronto")
    items = [
        mail.EventDigestItem(
            kind="scheduled",
            title="Sunday Mass",
            path="Liturgy / Lectors",
            slot="Lector",
            starts_at=datetime(2026, 8, 23, 10, 30, tzinfo=tz),
            ends_at=datetime(2026, 8, 23, 12, 0, tzinfo=tz),
            location="Main church",
        ),
        mail.EventDigestItem(
            kind="reminder",
            title="Bazaar shift",
            path="Bazaar Task Force",
            slot="Volunteers",
            starts_at=datetime(2026, 8, 25, 9, 0, tzinfo=tz),
            ends_at=datetime(2026, 8, 25, 11, 0, tzinfo=tz),
        ),
    ]
    subject, body = mail.event_digest_email(items, "https://vdb.example.org/events")
    assert subject == "VolunteerDB: your upcoming service"
    assert "You have been scheduled to serve:" in body
    assert "Coming up soon" in body
    assert "Sunday Mass — Lector (Liturgy / Lectors)" in body
    assert "Sunday, August 23, 2026, 10:30 AM–12:00 PM, Main church" in body
    assert "https://vdb.example.org/events" in body

    _, without_link = mail.event_digest_email(items, None)
    assert "https://" not in without_link, "no configured base URL, no link"


def test_sub_request_email_carries_the_note_and_claim_link():
    subject, body = mail.sub_request_email(
        "Sunday Mass",
        "Liturgy",
        "Lector",
        "Sunday, August 23, 2026, 10:30 AM–12:00 PM",
        "Mia Member",
        "out of town",
        "https://vdb.example.org/events",
    )
    assert subject == "Substitute needed: Sunday Mass"
    assert "Mia Member can no longer serve as Lector" in body
    assert "out of town" in body
    assert "https://vdb.example.org/events" in body


def test_event_when_spans_days_when_needed():
    tz = ZoneInfo("America/Toronto")
    same_day = mail.event_when(
        datetime(2026, 8, 23, 10, 30, tzinfo=tz),
        datetime(2026, 8, 23, 12, 0, tzinfo=tz),
    )
    assert same_day == "Sunday, August 23, 2026, 10:30 AM–12:00 PM"
    overnight = mail.event_when(
        datetime(2026, 8, 23, 22, 0, tzinfo=tz),
        datetime(2026, 8, 24, 2, 0, tzinfo=tz),
    )
    assert "August 23" in overnight and "August 24" in overnight
