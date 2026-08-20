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


def test_mail_names_the_organisation_when_one_is_configured(monkeypatch):
    from volunteerdb.config import settings

    monkeypatch.setenv("VDB_ORG_NAME", "St. Timothy's")
    settings.cache_clear()

    subject, body = mail.invite_email("https://vdb.example.org/invite/tok")
    assert subject == "Your VolunteerDB account at St. Timothy's"
    assert "the volunteer database for St. Timothy's." in body
    # Never a possessive: any name ending in s would produce "Timothy's's".
    assert "'s's" not in body and "'s's" not in subject

    _, confirm = mail.email_change_email(
        "https://vdb.example.org/confirm-email/tok", "new@example.org", 24
    )
    assert "the volunteer database for St. Timothy's, " in confirm
    settings.cache_clear()


def test_mail_reads_cleanly_with_no_organisation_set(monkeypatch):
    """The default is empty rather than a placeholder, so the copy has to
    survive it — no double spaces, no dangling "at", no orphaned dash."""
    from volunteerdb.config import settings

    monkeypatch.setenv("VDB_ORG_NAME", "")
    settings.cache_clear()

    subject, body = mail.invite_email("https://vdb.example.org/invite/tok")
    _, confirm = mail.email_change_email(
        "https://vdb.example.org/confirm-email/tok", "new@example.org", 24
    )

    assert subject == "Your VolunteerDB account"
    for text in (subject, body, confirm):
        assert "  " not in text, f"double space in {text!r}"
        assert " at ." not in text and text.rstrip() == text.rstrip()
    assert body.startswith("An account has been created for you in VolunteerDB, ")
    assert "in VolunteerDB, the volunteer database, to this one" in confirm
    settings.cache_clear()


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
            kind="week",
            title="Bazaar shift",
            path="Bazaar Task Force",
            slot="Volunteers",
            starts_at=datetime(2026, 8, 25, 9, 0, tzinfo=tz),
            ends_at=datetime(2026, 8, 25, 11, 0, tzinfo=tz),
        ),
        mail.EventDigestItem(
            kind="day",
            title="Vigil",
            path="Liturgy",
            slot="Sacristan",
            starts_at=datetime(2026, 8, 24, 19, 0, tzinfo=tz),
            ends_at=datetime(2026, 8, 24, 21, 0, tzinfo=tz),
        ),
    ]
    subject, body = mail.event_digest_email(items, "https://vdb.example.org/events")
    assert subject == "VolunteerDB: your upcoming service"
    assert "You have been scheduled to serve:" in body
    assert "Coming up this week" in body
    assert "Tomorrow — you are serving:" in body
    assert body.index("Tomorrow") < body.index("Coming up this week"), (
        "strongest notice first"
    )
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


def test_the_address_confirmation_names_the_address_the_link_and_the_window():
    subject, body = mail.email_change_email(
        "https://vdb.example.org/confirm-email/tok3n", "new@example.org", 24
    )
    assert subject == "Confirm your new VolunteerDB address"
    assert "https://vdb.example.org/confirm-email/tok3n" in body
    assert "new@example.org" in body, "the reader must see which address this is"
    assert "24 hours" in body
    # it goes to an address nobody has verified yet, so it must read as
    # declinable and must not leak who the account belongs to
    assert "ignore this email" in body


def test_the_old_address_is_warned_while_it_can_still_say_no():
    """SP 800-63B §4.1.2 wants the notice on a channel the transaction cannot
    suppress; sending it at *request* time is what makes it actionable."""
    subject, body = mail.email_change_requested_email(
        "new@example.org", "https://vdb.example.org/account", 24
    )
    assert subject == "Your VolunteerDB address is being changed"
    assert "new@example.org" in body, "it names the address being moved to"
    assert "https://vdb.example.org/account" in body, "and where to cancel it"
    assert "24 hours" in body
    assert "Nothing has changed yet" in body
    assert "parish office" in body, "and what to do if it was not you"


def test_the_old_address_gets_the_receipt_and_the_last_word():
    subject, body = mail.email_change_done_email(
        "new@example.org", "https://vdb.example.org/login"
    )
    assert subject == "Your VolunteerDB address changed"
    assert "new@example.org" in body
    assert "last message" in body, "this mailbox hears nothing from us again"
    assert "https://vdb.example.org/login" in body
