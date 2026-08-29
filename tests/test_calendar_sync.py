"""The Google Calendar reconcile: the calendar's own lifecycle (created once,
remembered, replaced when deleted, kept public), convergence, idempotence,
change detection, cancellation cleanup, orphan GC, hand-added entries, and
the failure paths. All Google traffic is monkeypatched at the gcal module
boundary — the job is what is under test, not the wire (that is
test_gcal_client.py)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.db import db_session
from volunteerdb.errors import External
from volunteerdb.fp import Err, Ok
from volunteerdb.jobs import calendar_sync
from volunteerdb.models import Event
from volunteerdb.services import events as event_service
from volunteerdb.services import gcal, teams

from tests import mint
from tests.fp_helpers import ok

TZ = ZoneInfo("America/Toronto")
CALENDAR_ID = "abc123@group.calendar.google.com"


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), TZ)


async def _team_and_event(title: str = "Sunday Mass") -> tuple[int, int]:
    async with db_session() as session:
        team = ok(await teams.create(session, None, "Altar Servers"))
        start = _at(date.today() + timedelta(days=7), 10)
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


class FakeGcal:
    """Records the calls the job makes; behaviours overridable per test. The
    database side of gcal (stored_calendar, remember, forget_pushes) runs for
    real — it is part of what the job is."""

    def __init__(self, monkeypatch):
        self.created: list[str] = []
        self.inserted: list[dict] = []
        self.patched: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.listed: list[dict] = []
        self.unmanaged: list[dict] = []
        self.verified: list[str] = []
        self.calendar_ids: list[str] = []  # what every event call addressed
        self.insert_error = False
        self.existing: set[str] = set()  # calendars Google still has
        self.problems: list[str] = []

        monkeypatch.setattr(gcal, "enabled", lambda cfg: True)

        async def mint_token(client, cfg) -> Ok[str]:
            return Ok("token")

        async def create_calendar(client, token: str, *, name: str, tz: str) -> Ok[str]:
            cid = CALENDAR_ID if not self.created else f"cal{len(self.created)}@g"
            self.created.append(cid)
            self.existing.add(cid)
            return Ok(cid)

        async def calendar_exists(client, token: str, cid: str) -> Ok[bool]:
            return Ok(cid in self.existing)

        async def verify_readonly(client, token: str, cid: str) -> Ok[list[str]]:
            self.verified.append(cid)
            return Ok(list(self.problems))

        async def insert(client, token: str, cid: str, payload: dict):
            self.calendar_ids.append(cid)
            if self.insert_error:
                return Err(External(gcal.SERVICE, "insert failed: HTTP 500"))
            self.inserted.append(payload)
            return Ok(f"g{len(self.inserted)}")

        async def patch(client, token: str, cid: str, gid: str, payload: dict):
            self.calendar_ids.append(cid)
            self.patched.append((gid, payload))
            return Ok(None)

        async def delete(client, token: str, cid: str, gid: str) -> Ok[None]:
            self.calendar_ids.append(cid)
            self.deleted.append(gid)
            return Ok(None)

        async def list_managed(client, token: str, cid: str, time_min: datetime):
            return Ok(self.listed)

        async def list_unmanaged(client, token: str, cid: str, time_min: datetime):
            return Ok(self.unmanaged)

        for name, fn in (
            ("mint_token", mint_token),
            ("create_calendar", create_calendar),
            ("calendar_exists", calendar_exists),
            ("verify_readonly", verify_readonly),
            ("insert", insert),
            ("patch", patch),
            ("delete", delete),
            ("list_managed", list_managed),
            ("list_unmanaged", list_unmanaged),
        ):
            monkeypatch.setattr(gcal, name, fn)


@pytest.fixture
def fake_gcal(monkeypatch):
    return FakeGcal(monkeypatch)


async def _stored(event_id: int) -> tuple[str | None, str | None]:
    async with db_session() as session:
        event = await session.get(Event, event_id)
        return event.google_event_id, event.google_fingerprint


async def _remembered() -> dict | None:
    async with db_session() as session:
        return await gcal.stored_calendar(session)


async def test_unconfigured_sync_is_a_quiet_noop(database, capsys, env):
    assert await calendar_sync.main(env) == 0
    assert "not configured" in capsys.readouterr().out


# --- the calendar itself ------------------------------------------------------


async def test_first_run_creates_the_calendar_and_remembers_it(
    database, fake_gcal, capsys, env
):
    assert await _remembered() is None
    assert await calendar_sync.main(env) == 0
    assert fake_gcal.created == [CALENDAR_ID]
    row = await _remembered()
    assert row["calendar_id"] == CALENDAR_ID
    assert row["created_at"] and row["verified_at"], (
        "created on this run, and its sharing checked clean on the same run"
    )
    assert fake_gcal.verified == [CALENDAR_ID]
    assert "ACL ok" in capsys.readouterr().out

    assert await calendar_sync.main(env) == 0
    assert fake_gcal.created == [CALENDAR_ID], "the second run reuses it"


async def test_a_deleted_calendar_is_replaced_and_everything_repushed(
    database, fake_gcal, log_records, env
):
    _, event_id = await _team_and_event()
    await calendar_sync.main(env)
    gid, _ = await _stored(event_id)
    assert gid == "g1" and fake_gcal.calendar_ids == [CALENDAR_ID]

    fake_gcal.existing.discard(CALENDAR_ID)  # somebody deleted it in Google
    assert await calendar_sync.main(env) == 0
    assert fake_gcal.created == [CALENDAR_ID, "cal1@g"]
    assert (await _remembered())["calendar_id"] == "cal1@g"
    gid, _ = await _stored(event_id)
    assert gid == "g2" and fake_gcal.calendar_ids[-1] == "cal1@g", (
        "the stale stamp was cleared, so the event was inserted afresh onto "
        "the replacement rather than patched on the calendar that is gone"
    )
    assert not fake_gcal.patched
    assert any(r["event"] == "calendar_sync.calendar_missing" for r in log_records)


async def test_a_sharing_problem_fails_the_run_but_still_syncs(
    database, fake_gcal, log_records, capsys, env
):
    _, event_id = await _team_and_event()
    fake_gcal.problems = ["user somebody@example.org may write"]
    assert await calendar_sync.main(env) == 1, "so the scheduler's alert mail fires"
    assert len(fake_gcal.inserted) == 1, "the events still went up"
    problem_logs = [r for r in log_records if r["event"] == "calendar_sync.acl_problem"]
    assert problem_logs and problem_logs[0]["problem"].startswith("user somebody")
    assert "1 ACL problem(s)" in capsys.readouterr().out
    assert "verified_at" not in (await _remembered()), (
        "a run that found a problem does not stamp the calendar verified"
    )


async def test_hand_added_entries_are_reported_not_removed(
    database, fake_gcal, log_records, capsys, env
):
    await _team_and_event()
    fake_gcal.unmanaged = [{"id": "byhand", "summary": "Parish retreat"}]
    assert await calendar_sync.main(env) == 0, "a warning, not a failure"
    assert "byhand" not in fake_gcal.deleted
    assert "1 unmanaged" in capsys.readouterr().out
    reports = [r for r in log_records if r["event"] == "calendar_sync.unmanaged_entry"]
    assert reports and reports[0]["summary"] == "Parish retreat"


# --- events -------------------------------------------------------------------


async def test_first_run_pushes_then_second_run_is_idle(database, fake_gcal, env):
    _, event_id = await _team_and_event()
    assert await calendar_sync.main(env) == 0
    assert [p["summary"] for p in fake_gcal.inserted] == ["Sunday Mass"]
    payload = fake_gcal.inserted[0]
    assert payload["location"] == "Main church"
    assert payload["extendedProperties"]["private"]["vdb_id"] == str(event_id)
    assert "attendees" not in payload and "description" not in payload
    gid, fp = await _stored(event_id)
    assert gid == "g1" and fp == gcal.fingerprint(payload)

    assert await calendar_sync.main(env) == 0
    assert len(fake_gcal.inserted) == 1 and not fake_gcal.patched, (
        "an unchanged event costs no API call"
    )


async def test_edit_patches_and_cancel_deletes(database, fake_gcal, env):
    _, event_id = await _team_and_event()
    await calendar_sync.main(env)

    async with db_session() as session:
        ok(
            await event_service.update_event(
                session, None, event_id, location="Parish Hall"
            )
        )
    await calendar_sync.main(env)
    assert [gid for gid, _ in fake_gcal.patched] == ["g1"]
    assert fake_gcal.patched[0][1]["location"] == "Parish Hall"

    async with db_session() as session:
        ok(
            await event_service.cancel_event(
                session, None, event_id, cancelled_by=None, now=mint.now()
            )
        )
    await calendar_sync.main(env)
    assert fake_gcal.deleted == ["g1"]
    assert await _stored(event_id) == (None, None)


async def test_orphaned_calendar_entries_are_collected(database, fake_gcal, env):
    await _team_and_event()
    fake_gcal.listed = [
        {
            "id": "gone",
            "extendedProperties": {"private": {"vdb_id": "99999", "vdb_managed": "1"}},
        }
    ]
    await calendar_sync.main(env)
    assert fake_gcal.deleted == ["gone"], (
        "a managed entry with no live event behind it (team CASCADE) is removed"
    )


async def test_live_events_survive_the_gc_sweep(database, fake_gcal, env):
    _, event_id = await _team_and_event()
    await calendar_sync.main(env)
    fake_gcal.listed = [
        {
            "id": "g1",
            "extendedProperties": {
                "private": {"vdb_id": str(event_id), "vdb_managed": "1"}
            },
        }
    ]
    await calendar_sync.main(env)
    assert fake_gcal.deleted == []


async def test_failed_insert_leaves_no_stamp_and_exits_nonzero(
    database, fake_gcal, env
):
    _, event_id = await _team_and_event()
    fake_gcal.insert_error = True
    assert await calendar_sync.main(env) == 1
    assert await _stored(event_id) == (None, None), (
        "NULL stamps mean the next run retries the push"
    )
    fake_gcal.insert_error = False
    assert await calendar_sync.main(env) == 0
    assert len(fake_gcal.inserted) == 1


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
