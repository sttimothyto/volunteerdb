"""The SMTP2GO transport via a mocked httpx client -- the 'never raises'
contract -- and the parish copy the templates build."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from volunteerdb.env import LoggingMailer, Smtp2goMailer
from volunteerdb.services import mail

from tests import mint

pytestmark = pytest.mark.pure

SETTINGS = SimpleNamespace(
    smtp2go_api_key="test-key",
    mail_from="vdb@example.org",
    mail_from_name="VolunteerDB",
    timezone="America/Toronto",
)


class _Http:
    """An HttpClients whose every client answers with `handler`."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def client(self, *, timeout: float = 10.0, follow_redirects: bool = False):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handler),
            timeout=timeout,
            follow_redirects=follow_redirects,
        )


class _Quota:
    """A QuotaCell that only remembers what it was asked to count."""

    def __init__(self) -> None:
        self.counted: list[object] = []

    async def record(self, sessions, day) -> None:
        self.counted.append(day)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _mailer(handler, quota: _Quota | None = None) -> Smtp2goMailer:
    return Smtp2goMailer(
        SETTINGS, _Http(handler), sessions=None, quota=quota or _Quota(), clock=_Clock()
    )


async def test_send_email_posts_to_smtp2go():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"succeeded": 1}})

    assert await _mailer(handler).send("to@example.org", "Hello", "Body text") is True
    (request,) = seen
    assert str(request.url) == mail.API_URL
    assert request.headers["X-Smtp2go-Api-Key"] == "test-key"
    payload = json.loads(request.content)
    assert payload["sender"] == "VolunteerDB <vdb@example.org>"
    assert payload["to"] == ["to@example.org"]
    assert payload["subject"] == "Hello"
    assert payload["text_body"] == "Body text"


def _refused(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "boom"})


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route", request=request)


def _nobody_succeeded(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": {"succeeded": 0}})


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(_refused, id="the API refused it"),
        pytest.param(_unreachable, id="the API was unreachable"),
        pytest.param(_nobody_succeeded, id="200, but nobody succeeded"),
    ],
)
async def test_a_send_that_did_not_go_through_is_false_and_uncounted(handler):
    """Three ways a message fails to leave; one answer, and no allowance spent.
    A ledger that counted attempts would shout loudest exactly when nothing
    was getting through."""
    quota = _Quota()
    assert await _mailer(handler, quota).send("to@example.org", "s", "b") is False
    assert quota.counted == []


async def test_every_message_that_leaves_is_counted():
    """The provider stops delivering at 200 a day / 1,000 a month and tells
    nobody in advance; counting our own sends is the only warning there is."""
    quota = _Quota()
    mailer = _mailer(
        lambda request: httpx.Response(200, json={"data": {"succeeded": 1}}), quota
    )
    assert await mailer.send("to@example.org", "s", "b") is True
    assert len(quota.counted) == 1


async def test_an_unconfigured_instance_counts_nothing(capsys):
    """No API key means nothing left the building — dev, tests, the verify
    harness. The allowance is untouched, and nothing on this path needs a
    database."""
    mailer = LoggingMailer(SimpleNamespace(debug_mail=True, reload=False))
    assert await mailer.send("to@example.org", "s", "b") is True
    assert "[MAIL]" in capsys.readouterr().out


def test_ttl_window_renders_whole_days_as_days():
    assert mail.ttl_window(24) == "24 hours"
    assert mail.ttl_window(36) == "36 hours"
    assert mail.ttl_window(48) == "2 days"
    assert mail.ttl_window(168) == "7 days"


def test_invite_email_states_the_default_week():
    _, body = mail.invite_email(
        "https://vdb.example.org/invite/tok", ctx=mint.mail_context()
    )
    assert "7 days" in body, "the 168-hour default must read as days"


def test_mail_names_the_organisation_when_one_is_configured():
    parish = mint.mail_context(org="St. Timothy's")

    subject, body = mail.invite_email("https://vdb.example.org/invite/tok", ctx=parish)
    assert subject == "Your VolunteerDB account at St. Timothy's"
    assert "the volunteer database for St. Timothy's." in body
    # Never a possessive: any name ending in s would produce "Timothy's's".
    assert "'s's" not in body and "'s's" not in subject

    _, confirm = mail.email_change_email(
        "https://vdb.example.org/confirm-email/tok", "new@example.org", 24, ctx=parish
    )
    assert "the volunteer database for St. Timothy's, " in confirm


def test_mail_reads_cleanly_with_no_organisation_set():
    """The default is empty rather than a placeholder, so the copy has to
    survive it — no double spaces, no dangling "at", no orphaned dash."""
    subject, body = mail.invite_email(
        "https://vdb.example.org/invite/tok", ctx=mint.mail_context()
    )
    _, confirm = mail.email_change_email(
        "https://vdb.example.org/confirm-email/tok",
        "new@example.org",
        24,
        ctx=mint.mail_context(),
    )

    assert subject == "Your VolunteerDB account"
    for text in (subject, body, confirm):
        assert "  " not in text, f"double space in {text!r}"
        assert " at ." not in text and text.rstrip() == text.rstrip()
    assert body.startswith("An account has been created for you in VolunteerDB, ")
    assert "in VolunteerDB, the volunteer database, to this one" in confirm


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
    subject, body = mail.event_digest_email(
        items, "https://vdb.example.org/events", tz=mint.tz()
    )
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

    _, without_link = mail.event_digest_email(items, None, tz=mint.tz())
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
        tz=mint.tz(),
    )
    assert same_day == "Sunday, August 23, 2026, 10:30 AM–12:00 PM"
    overnight = mail.event_when(
        datetime(2026, 8, 23, 22, 0, tzinfo=tz),
        datetime(2026, 8, 24, 2, 0, tzinfo=tz),
        tz=mint.tz(),
    )
    assert "August 23" in overnight and "August 24" in overnight


def test_the_address_confirmation_names_the_address_the_link_and_the_window():
    subject, body = mail.email_change_email(
        "https://vdb.example.org/confirm-email/tok3n",
        "new@example.org",
        24,
        ctx=mint.mail_context(),
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


def test_a_leaders_correction_warns_the_old_address_and_names_the_new_one():
    subject, body = mail.address_edited_email("new@example.org", "https://x/login")
    assert subject == "Your VolunteerDB address was changed"
    assert "new@example.org" in body
    assert "sign in at https://x/login" in body
    assert "tell the parish office" in body
    assert "last message we will send to this address" in body
    _s, without = mail.address_edited_email("new@example.org")
    assert "sign in at" not in without, "no public URL, no link"


def test_the_digest_groups_by_what_changed_and_never_links():
    items = [
        mail.DigestItem(
            "added", "Second — Liturgy", date(2026, 9, 10), date(2026, 9, 17)
        ),
        mail.DigestItem("voting", "Leader — Choir", date(2026, 9, 1), date(2026, 9, 8)),
        mail.DigestItem("both", "Second — Ushers", date(2026, 9, 2), date(2026, 9, 9)),
    ]
    subject, body = mail.proposal_digest_email(items)
    assert "You have been added to the voting roll for:" in body
    assert "Voting is now open for:" in body
    assert "already open" in body
    assert body.index("Liturgy") < body.index("Choir") < body.index("Ushers"), (
        "sections in the fixed order: added, voting, both"
    )
    assert "http" not in body, "the job has no request to derive a link from"


def test_the_asker_hears_who_took_their_slot():
    subject, body = mail.sub_claimed_email("Mass", "Lector", "Sun 10:00", "Noor", "Mia")
    assert subject == "Substitute found: Mass"
    assert "Noor has taken over Mia's Lector slot" in body
    assert "Sun 10:00" in body


def test_the_incoming_volunteer_is_told_and_given_a_way_out():
    subject, body = mail.substituted_in_email(
        "Mass", "Lector", "Sun 10:00", "Mia", "https://x/events"
    )
    assert subject == "You're now serving: Mass"
    assert "Mia has handed you their Lector slot" in body
    assert body.rstrip().endswith("https://x/events")


def test_leaders_hear_a_self_removal_with_the_reason_verbatim():
    subject, body = mail.self_removal_email(
        "Mass", "Liturgy / Music", "Lector", "Sun 10:00", "Mia", "Away at a <wedding>"
    )
    assert subject == "Off the roster: Mass"
    assert "Their reason: Away at a <wedding>" in body, (
        "quoted as typed; it is plain text"
    )
    assert "Liturgy / Music" in body and "Sun 10:00" in body


def test_a_cancellation_tells_the_assignee_nothing_is_needed():
    subject, body = mail.event_cancelled_email("Mass", "Liturgy", "Sun 10:00")
    assert subject == "Cancelled: Mass"
    assert "Mass (Liturgy)" in body and "Sun 10:00" in body
    assert "no action is needed" in body
