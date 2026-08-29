"""policy.plan: what a domain event implies, decided purely.

Every rule is checked with values only -- a PolicyCtx built by hand, a
throttle ledger as a value, no database, no clock -- which is the point of
the split: the two doors (GUI, JSON API) run these same rules over the same
events, and this file is where their agreement is pinned."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb import policy, throttle
from volunteerdb.domain import (
    EventCancelled,
    NotifyMode,
    SelfRemoved,
    SlotHandedOver,
    SubClaimed,
    SubRequested,
)
from volunteerdb.effects import Audit, SendMail, ThrottleHit
from volunteerdb.services import mail

pytestmark = pytest.mark.pure

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
START = NOW + timedelta(days=3)
END = START + timedelta(hours=2)
COPY = mail.MailContext(org="St Timothy", invite_ttl_hours=72, tz=ZoneInfo("UTC"))


def _ctx(
    *, notify: NotifyMode = NotifyMode.direct, ledger: throttle.Ledger | None = None
) -> policy.PolicyCtx:
    return policy.PolicyCtx(
        now=NOW,
        base_url="https://vdb.example",
        notify=notify,
        throttle=ledger if ledger is not None else throttle.Ledger(),
        copy=COPY,
    )


def _sub_requested(**changes) -> SubRequested:
    base = dict(
        team_id=7,
        event_id=3,
        title="Sunday Mass",
        path="Liturgy",
        slot="Lector",
        starts_at=START,
        ends_at=END,
        asker="Mia Member",
        note="out of town",
        audience=("lena@example.org", "noor@example.org"),
    )
    return SubRequested(**{**base, **changes})


def _mails(effects) -> list[SendMail]:
    return [e for e in effects if isinstance(e, SendMail)]


# --- a substitute request ---------------------------------------------------


def test_a_sub_request_charges_the_team_and_mails_the_audience():
    effects = policy.plan([_sub_requested()], _ctx())
    assert effects[0] == ThrottleHit("sub-req:7")
    mails = _mails(effects)
    assert [m.to for m in mails] == ["lena@example.org", "noor@example.org"]
    assert "Substitute needed" in mails[0].subject
    assert "out of town" in mails[0].body and "Mia Member" in mails[0].body
    assert "https://vdb.example/events" in mails[0].body, "the link is edge data"


def test_a_capped_team_gets_the_row_and_no_mail():
    """The request is never refused; the blast is. The ledger snapshot says
    whether this team has sent its allowance today."""
    ledger = throttle.Ledger()
    limit = throttle.LIMITS["sub-req"]
    for _ in range(limit.hits):
        ledger = throttle.hit(ledger, "sub-req:7", NOW - timedelta(hours=1))
    effects = policy.plan([_sub_requested()], _ctx(ledger=ledger))
    assert not _mails(effects) and not any(isinstance(e, ThrottleHit) for e in effects)
    assert effects == (
        Audit("event.sub_request_capped", (("event_id", 3), ("team_id", 7))),
    )


def test_an_empty_audience_is_still_a_charge_but_no_mail():
    effects = policy.plan([_sub_requested(audience=())], _ctx())
    assert effects == (ThrottleHit("sub-req:7"),)


def test_the_api_door_announces_no_sub_request():
    """NotifyMode.digest: the JSON API sends no roster mail of its own."""
    assert policy.plan([_sub_requested()], _ctx(notify=NotifyMode.digest)) == ()


# --- a hand-off -------------------------------------------------------------


def _handed_over(**changes) -> SlotHandedOver:
    base = dict(
        event_id=3,
        assignment_id=11,
        title="Sunday Mass",
        slot="Lector",
        starts_at=START,
        ends_at=END,
        outgoing_id=1,
        outgoing_name="Mia Member",
        incoming_id=2,
        incoming_email="noor@example.org",
        notify=NotifyMode.direct,
    )
    return SlotHandedOver(**{**base, **changes})


def test_a_hand_off_is_logged_and_the_incoming_volunteer_is_told():
    effects = policy.plan([_handed_over()], _ctx())
    assert effects[0] == Audit(
        "event.slot_handed_over",
        (
            ("event_id", 3),
            ("assignment_id", 11),
            ("from_volunteer_id", 1),
            ("to_volunteer_id", 2),
        ),
    )
    (m,) = _mails(effects)
    assert m.to == "noor@example.org" and "You're now serving" in m.subject
    assert "Mia Member" in m.body


def test_a_digest_hand_off_is_logged_but_not_mailed():
    """The service stamped no 'scheduled' notice for a digest hand-off, so
    the nightly digest tells the incoming volunteer; mailing too would
    double up. The mode rides the event, not the door."""
    effects = policy.plan([_handed_over(notify=NotifyMode.digest)], _ctx())
    assert [type(e) for e in effects] == [Audit]


def test_a_hand_off_to_someone_without_an_address_is_only_logged():
    effects = policy.plan([_handed_over(incoming_email=None)], _ctx())
    assert [type(e) for e in effects] == [Audit]


# --- a claim, a self-removal, a cancellation ---------------------------------


def test_a_claim_tells_the_asker_alone():
    event = SubClaimed(
        event_id=3,
        sub_request_id=5,
        title="Sunday Mass",
        slot="Lector",
        starts_at=START,
        ends_at=END,
        claimant="Noor Member",
        asker="Mia Member",
        asker_email="mia@example.org",
    )
    effects = policy.plan([event], _ctx())
    (m,) = effects
    assert isinstance(m, SendMail) and m.to == "mia@example.org"
    assert "Substitute found" in m.subject and "Noor Member" in m.body
    assert policy.plan([event], _ctx(notify=NotifyMode.digest)) == ()


def test_a_self_removal_is_logged_and_the_leaders_hear_the_reason():
    event = SelfRemoved(
        event_id=3,
        title="Sunday Mass",
        path="Liturgy",
        slot="Lector",
        starts_at=START,
        ends_at=END,
        who="Noor Member",
        volunteer_id=2,
        reason="travelling that weekend",
        leader_emails=("lena@example.org",),
    )
    effects = policy.plan([event], _ctx())
    assert effects[0] == Audit(
        "event.self_removal",
        (("event_id", 3), ("volunteer_id", 2), ("reason", "travelling that weekend")),
    )
    (m,) = _mails(effects)
    assert m.to == "lena@example.org" and "Off the roster" in m.subject
    assert "travelling that weekend" in m.body
    assert [type(e) for e in policy.plan([event], _ctx(notify=NotifyMode.digest))] == [
        Audit
    ], "the API door logs it too, and mails nobody"


def test_a_cancellation_mails_the_assignees_unless_the_event_was_over():
    upcoming = EventCancelled(
        event_id=3,
        title="Sunday Mass",
        path="Liturgy",
        starts_at=START,
        ends_at=END,
        emails=("mia@example.org", "noor@example.org"),
    )
    mails = _mails(policy.plan([upcoming], _ctx()))
    assert [m.to for m in mails] == ["mia@example.org", "noor@example.org"]
    assert "cancelled" in mails[0].subject.lower()
    past = EventCancelled(
        event_id=3,
        title="Sunday Mass",
        path="Liturgy",
        starts_at=NOW - timedelta(days=2),
        ends_at=NOW - timedelta(days=2, hours=-2),
        emails=("mia@example.org",),
    )
    assert policy.plan([past], _ctx()) == (), "nobody needs mail about a past event"
    assert policy.plan([upcoming], _ctx(notify=NotifyMode.digest)) == ()


def test_plan_keeps_event_order():
    effects = policy.plan([_handed_over(), _sub_requested()], _ctx())
    kinds = [type(e).__name__ for e in effects]
    assert kinds == ["Audit", "SendMail", "ThrottleHit", "SendMail", "SendMail"]
