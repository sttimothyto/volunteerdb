"""The Google Calendar reconcile: the calendar's own lifecycle (created once,
remembered, replaced when deleted, kept public), convergence, idempotence,
change detection, cancellation cleanup, orphan GC, hand-added entries, and
the failure paths. Google is an in-memory calendar behind the Env's HTTP
client (tests/fakes.py FakeGoogleCalendar): nothing is patched, and a test
reads back what the calendar holds. The wire itself is test_gcal_client.py's."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.jobs import calendar_sync
from volunteerdb.models import Event
from volunteerdb.services import events as event_service
from volunteerdb.services import gcal, teams

from tests import mint
from tests.conftest import db_session
from tests.fakes import FakeGoogleCalendar
from tests.fp_helpers import ok

TZ = ZoneInfo("America/Toronto")
CALENDAR_ID = "abc123@group.calendar.google.com"


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), TZ)


async def _team_and_event(title: str = "Sunday Mass") -> tuple[int, int]:
    async with db_session() as session:
        team = ok(await teams.create(session, None, "Altar Servers"))
        start = _at(mint.today() + timedelta(days=7), 10)
        created = ok(
            await event_service.create_event(
                session,
                None,
                team_id=team.id,
                title=title,
                starts_at=start,
                ends_at=start + timedelta(hours=2),
                location="Main church",
                created_by=None,
                tz=mint.tz(),
                series_id=mint.uuid(),
            )
        )
        return team.id, created[0].id


@pytest.fixture
def google() -> FakeGoogleCalendar:
    """Google Calendar, in memory, behind the Env's HTTP client: the job
    reaches it exactly as it reaches Google, and a test reads back what the
    calendar now holds rather than what a stub was told to say."""
    return FakeGoogleCalendar()


@pytest.fixture
def genv(env, google):
    """The Env with the parish Google grant configured and its HTTP routed to
    the fake. The database side of gcal (stored_calendar, remember,
    forget_pushes) runs for real -- it is part of what the job is."""
    configured = env.settings.model_copy(
        update={
            "sheets_client_id": "cid",
            "sheets_client_secret": "secret",
            "sheets_refresh_token": "refresh",
        }
    )
    return env.with_(settings=configured, http=google.http)


async def _remembered_calendar(google: FakeGoogleCalendar, env) -> str:
    """A calendar Google already has and the app already remembers -- the
    state after a first run, for tests that need entries on it beforehand."""
    cid = google.create(CALENDAR_ID)
    async with db_session() as session:
        await gcal.remember(session, cid, now=env.clock.now(), created=True)
    return cid


def _events_of(google: FakeGoogleCalendar, cid: str) -> dict[str, dict]:
    return google.calendars[cid]["events"]


async def _stored(event_id: int) -> tuple[str | None, str | None]:
    async with db_session() as session:
        event = await session.get(Event, event_id)
        return event.google_event_id, event.google_fingerprint


async def _remembered() -> dict | None:
    async with db_session() as session:
        return await gcal.stored_calendar(session)


async def test_unconfigured_sync_is_a_quiet_noop(database, capsys, env):
    unconfigured = env.settings.model_copy(
        update={
            "sheets_client_id": "",
            "sheets_client_secret": "",
            "sheets_refresh_token": "",
        }
    )
    assert await calendar_sync.main(env.with_(settings=unconfigured)) == 0
    assert "not configured" in capsys.readouterr().out


# --- the calendar itself ------------------------------------------------------


async def test_first_run_creates_the_calendar_and_remembers_it(
    database, google, capsys, genv
):
    assert await _remembered() is None
    assert await calendar_sync.main(genv) == 0
    (cid,) = google.created
    row = await _remembered()
    assert row["calendar_id"] == cid
    assert row["created_at"] and row["verified_at"], (
        "created on this run, and its sharing checked clean on the same run"
    )
    assert len(google.requests("GET", "/acl")) == 1, "the sharing was checked"
    assert {r["scope"]["type"] for r in google.calendars[cid]["acl"]} == {
        "user",
        "default",
    }, "and the public read rule was added"
    assert "ACL ok" in capsys.readouterr().out

    assert await calendar_sync.main(genv) == 0
    assert google.created == [cid], "the second run reuses it"


async def test_a_deleted_calendar_is_replaced_and_everything_repushed(
    database, google, log_records, genv
):
    _, event_id = await _team_and_event()
    await calendar_sync.main(genv)
    (first,) = google.created
    gid, _ = await _stored(event_id)
    assert gid == "g1" and gid in _events_of(google, first)

    google.delete_calendar(first)  # somebody deleted it in Google
    assert await calendar_sync.main(genv) == 0
    assert len(google.created) == 2
    replacement = google.created[-1]
    assert (await _remembered())["calendar_id"] == replacement
    gid, _ = await _stored(event_id)
    assert gid == "g2" and gid in _events_of(google, replacement), (
        "the stale stamp was cleared, so the event was inserted afresh onto "
        "the replacement rather than patched on the calendar that is gone"
    )
    assert not google.requests("PATCH", "/events/g1")
    assert any(r["event"] == "calendar_sync.calendar_missing" for r in log_records)


async def test_a_sharing_problem_fails_the_run_but_still_syncs(
    database, google, log_records, capsys, genv
):
    _, event_id = await _team_and_event()
    google.problem_rules = [
        {
            "id": "user:somebody@example.org",
            "role": "writer",
            "scope": {"type": "user", "value": "somebody@example.org"},
        }
    ]
    assert await calendar_sync.main(genv) == 1, "so the scheduler's alert mail fires"
    (cid,) = google.created
    assert len(_events_of(google, cid)) == 1, "the events still went up"
    problem_logs = [r for r in log_records if r["event"] == "calendar_sync.acl_problem"]
    assert problem_logs and problem_logs[0]["problem"].startswith("user somebody")
    assert "1 ACL problem(s)" in capsys.readouterr().out
    assert "verified_at" not in (await _remembered()), (
        "a run that found a problem does not stamp the calendar verified"
    )


async def test_hand_added_entries_are_reported_not_removed(
    database, google, log_records, capsys, genv
):
    await _team_and_event()
    cid = await _remembered_calendar(google, genv)
    google.add_event(cid, {"id": "byhand", "summary": "Parish retreat"})
    assert await calendar_sync.main(genv) == 0, "a warning, not a failure"
    assert "byhand" in _events_of(google, cid), "left where it is"
    assert "1 unmanaged" in capsys.readouterr().out
    reports = [r for r in log_records if r["event"] == "calendar_sync.unmanaged_entry"]
    assert reports and reports[0]["summary"] == "Parish retreat"


# --- events -------------------------------------------------------------------


async def test_first_run_pushes_then_second_run_is_idle(database, google, genv):
    _, event_id = await _team_and_event()
    assert await calendar_sync.main(genv) == 0
    (cid,) = google.created
    (entry,) = _events_of(google, cid).values()
    assert entry["summary"] == "Sunday Mass"
    assert entry["location"] == "Main church"
    assert entry["extendedProperties"]["private"]["vdb_id"] == str(event_id)
    assert "attendees" not in entry and "description" not in entry
    gid, fp = await _stored(event_id)
    payload = {k: v for k, v in entry.items() if k != "id"}
    assert gid == "g1" and fp == gcal.fingerprint(payload)

    before = len(google.http.seen)
    assert await calendar_sync.main(genv) == 0
    writes = [
        r for r in google.http.seen[before:] if r.method in ("POST", "PATCH", "DELETE")
    ]
    assert [str(r.url).rsplit("/", 1)[-1] for r in writes] == ["token"], (
        "an unchanged event costs no API call: the second run only minted a token"
    )


async def test_edit_patches_and_cancel_deletes(database, google, genv):
    _, event_id = await _team_and_event()
    await calendar_sync.main(genv)
    (cid,) = google.created

    async with db_session() as session:
        ok(
            await event_service.update_event(
                session, None, event_id, location="Parish Hall"
            )
        )
    await calendar_sync.main(genv)
    assert len(google.requests("PATCH", "/events/g1")) == 1
    assert _events_of(google, cid)["g1"]["location"] == "Parish Hall"
    assert len(_events_of(google, cid)) == 1, "patched in place, not re-inserted"

    async with db_session() as session:
        ok(
            await event_service.cancel_event(
                session, None, event_id, cancelled_by=None, now=mint.now()
            )
        )
    await calendar_sync.main(genv)
    assert _events_of(google, cid) == {}, "the entry is gone from the calendar"
    assert await _stored(event_id) == (None, None)


async def test_orphaned_calendar_entries_are_collected(database, google, genv):
    await _team_and_event()
    cid = await _remembered_calendar(google, genv)
    google.add_event(
        cid,
        {
            "id": "gone",
            "extendedProperties": {"private": {"vdb_id": "99999", "vdb_managed": "1"}},
        },
    )
    await calendar_sync.main(genv)
    assert "gone" not in _events_of(google, cid), (
        "a managed entry with no live event behind it (team CASCADE) is removed"
    )
    assert len(_events_of(google, cid)) == 1, "the live event's entry went up"


async def test_live_events_survive_the_gc_sweep(database, google, genv):
    _, event_id = await _team_and_event()
    await calendar_sync.main(genv)
    (cid,) = google.created
    assert _events_of(google, cid)["g1"]["extendedProperties"]["private"][
        "vdb_id"
    ] == str(event_id)
    await calendar_sync.main(genv)
    assert "g1" in _events_of(google, cid)
    assert not google.requests("DELETE", "/events/g1")


async def test_failed_insert_leaves_no_stamp_and_exits_nonzero(
    database, google, genv, monkeypatch
):
    _, event_id = await _team_and_event()
    google.insert_status = 500
    assert await calendar_sync.main(genv) == 1
    assert await _stored(event_id) == (None, None), (
        "NULL stamps mean the next run retries the push"
    )
    google.insert_status = None
    assert await calendar_sync.main(genv) == 0
    (cid,) = google.created
    assert len(_events_of(google, cid)) == 1


def test_fingerprint_ignores_key_order():
    a = {"summary": "Mass", "start": {"dateTime": "x", "timeZone": "y"}}
    b = {"start": {"timeZone": "y", "dateTime": "x"}, "summary": "Mass"}
    assert gcal.fingerprint(a) == gcal.fingerprint(b)
    assert gcal.fingerprint(a) != gcal.fingerprint({**a, "summary": "Vespers"})


def test_the_calendars_public_faces_carry_the_id():
    cid = "parish@group.calendar.google.com"
    assert gcal.embed_url(cid, "America/Toronto").startswith(
        "https://calendar.google.com/calendar/embed?src=parish%40group"
    )
    assert gcal.public_url(cid) == (
        "https://calendar.google.com/calendar/r?cid=parish%40group.calendar.google.com"
    )
    assert gcal.google_ics_url(cid).endswith("/public/basic.ics")
