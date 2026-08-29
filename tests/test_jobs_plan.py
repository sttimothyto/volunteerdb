"""The jobs' plans, pure: who is told what tonight, and what the calendar
needs -- decided from values, without a database."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.jobs import calendar_sync, event_reminders, proposal_digest
from volunteerdb.models import ROLE_LABELS, NotificationStage, TeamRole
from volunteerdb.services import gcal

pytestmark = pytest.mark.pure

TZ = ZoneInfo("America/Toronto")
TODAY = date(2026, 9, 1)
PATHS = {7: "Liturgy"}


def _row(
    assignment_id=1,
    *,
    days_ahead: int,
    notify_7d=False,
    notify_24h=True,
    volunteer_id=10,
    email="mia@example.org",
):
    starts = datetime.combine(
        TODAY + timedelta(days=days_ahead), datetime.min.time(), TZ
    ).replace(hour=10)
    return event_reminders.ReminderRow(
        assignment_id=assignment_id,
        volunteer_id=volunteer_id,
        email=email,
        notify_7d=notify_7d,
        notify_24h=notify_24h,
        team_id=7,
        title="Sunday Mass",
        slot="Lector",
        starts_at=starts.astimezone(UTC),
        ends_at=(starts + timedelta(hours=2)).astimezone(UTC),
        location=None,
    )


def test_a_fresh_assignment_gets_the_scheduling_notice_and_that_stage_alone():
    (digest,) = event_reminders.plan(
        [_row(days_ahead=10)], PATHS, frozenset(), today=TODAY, tz=TZ
    )
    assert digest.email == "mia@example.org"
    assert [i.kind for i in digest.items] == ["scheduled"]
    assert digest.stamps == ((1, NotificationStage.event_scheduled),), (
        "no reminder window is here yet, so nothing else is settled"
    )


def test_tomorrow_records_every_satisfied_stage_under_the_strongest_heading():
    already = frozenset({(1, NotificationStage.event_scheduled)})
    (digest,) = event_reminders.plan(
        [_row(days_ahead=1, notify_7d=True)], PATHS, already, today=TODAY, tz=TZ
    )
    assert [i.kind for i in digest.items] == ["day"]
    assert set(digest.stamps) == {
        (1, NotificationStage.event_week),
        (1, NotificationStage.event_day),
    }


def test_an_opted_out_stage_never_fires_and_a_told_person_is_silent():
    already = frozenset({(1, NotificationStage.event_scheduled)})
    assert (
        event_reminders.plan(
            [_row(days_ahead=5, notify_7d=False)], PATHS, already, today=TODAY, tz=TZ
        )
        == []
    )
    (digest,) = event_reminders.plan(
        [_row(days_ahead=5, notify_7d=True)], PATHS, already, today=TODAY, tz=TZ
    )
    assert [i.kind for i in digest.items] == ["week"]


def test_one_digest_per_person_covers_every_event():
    rows = [
        _row(1, days_ahead=3),
        _row(2, days_ahead=8),
        _row(3, days_ahead=8, volunteer_id=11, email="noor@example.org"),
    ]
    digests = event_reminders.plan(rows, PATHS, frozenset(), today=TODAY, tz=TZ)
    assert [(d.email, len(d.items)) for d in digests] == [
        ("mia@example.org", 2),
        ("noor@example.org", 1),
    ]
    assert digests[0].items[0].path == "Liturgy"


def _voter(
    voter_id=1,
    *,
    volunteer_id=10,
    email="lena@example.org",
    nominating_until=TODAY + timedelta(days=5),
    voting_until=TODAY + timedelta(days=12),
):
    return proposal_digest.VoterRow(
        voter_id=voter_id,
        volunteer_id=volunteer_id,
        email=email,
        team_id=7,
        role=TeamRole.leader,
        nomination_deadline=nominating_until,
        voting_deadline=voting_until,
    )


def test_a_new_voter_is_told_once_and_told_again_when_voting_opens():
    (digest,) = proposal_digest.plan([_voter()], PATHS, frozenset(), today=TODAY)
    assert [i.kind for i in digest.items] == ["added"] and digest.stamps == (
        (1, NotificationStage.roll_added),
    )
    told = frozenset({(1, NotificationStage.roll_added)})
    assert proposal_digest.plan([_voter()], PATHS, told, today=TODAY) == []
    (digest,) = proposal_digest.plan(
        [_voter()], PATHS, told, today=TODAY + timedelta(days=6)
    )
    assert [i.kind for i in digest.items] == ["voting"] and digest.stamps == (
        (1, NotificationStage.voting_open),
    )


def test_added_and_voting_on_the_same_night_is_one_combined_item_and_concluded_is_silent():
    (digest,) = proposal_digest.plan(
        [_voter()], PATHS, frozenset(), today=TODAY + timedelta(days=6)
    )
    assert [i.kind for i in digest.items] == ["both"]
    assert set(digest.stamps) == {
        (1, NotificationStage.roll_added),
        (1, NotificationStage.voting_open),
    }
    assert digest.items[0].seat == f"{ROLE_LABELS[TeamRole.leader]} — Liturgy"
    assert (
        proposal_digest.plan(
            [_voter()], PATHS, frozenset(), today=TODAY + timedelta(days=20)
        )
        == []
    )


def _local(id=1, *, scheduled=True, google_id=None, fingerprint=None, summary="Mass"):
    payload = {"summary": summary}
    return calendar_sync.LocalEvent(
        id=id,
        scheduled=scheduled,
        google_id=google_id,
        fingerprint=fingerprint,
        payload=payload,
    )


def test_the_calendar_plan_inserts_patches_deletes_and_leaves_the_unchanged_alone():
    current = gcal.fingerprint({"summary": "Mass"})
    local = [
        _local(1),
        _local(2, google_id="g2", fingerprint="stale"),
        _local(3, google_id="g3", fingerprint=current),
        _local(4, scheduled=False, google_id="g4"),
        _local(5, scheduled=False),
    ]
    ops = calendar_sync.plan_ops(local)
    assert [type(op).__name__ for op in ops] == ["Insert", "Patch", "Delete"]
    assert ops[1] == calendar_sync.Patch(2, "g2", {"summary": "Mass"})
    assert calendar_sync.live_ids(local) == {"1", "2", "3"}


def test_orphans_are_the_managed_entries_with_no_live_event_behind_them():
    remote = [
        {"id": "keep", "extendedProperties": {"private": {"vdb_id": "1"}}},
        {"id": "gone", "extendedProperties": {"private": {"vdb_id": "99"}}},
        {
            "extendedProperties": {"private": {"vdb_id": "98"}}
        },  # no id: nothing to delete
    ]
    assert calendar_sync.orphans(remote, {"1"}) == ["gone"]
