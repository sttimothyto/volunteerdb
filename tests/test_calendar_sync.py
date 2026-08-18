"""The Google Calendar reconcile: convergence, idempotence, change
detection, cancellation cleanup, orphan GC, and the failure paths. All
Google traffic is monkeypatched at the gcal module boundary — the job is
what is under test, not the wire."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.config import settings
from volunteerdb.db import db_session
from volunteerdb.jobs import calendar_sync
from volunteerdb.models import Event
from volunteerdb.services import events as event_service
from volunteerdb.services import gcal, teams

TZ = ZoneInfo("America/Toronto")


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), TZ)


async def _team_and_event(title: str = "Sunday Mass") -> tuple[int, int]:
    async with db_session() as session:
        team = await teams.create(session, None, "Altar Servers")
        start = _at(date.today() + timedelta(days=7), 10)
        created = await event_service.create_event(
            session,
            team_id=team.id,
            title=title,
            starts_at=start,
            ends_at=start + timedelta(hours=2),
            location="Main church",
            created_by=None,
        )
        return team.id, created[0].id


class FakeGcal:
    """Records the calls the job makes; behaviours overridable per test."""

    def __init__(self, monkeypatch):
        self.inserted: list[dict] = []
        self.patched: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.listed: list[dict] = []
        self.insert_error = False
        cfg = settings().model_copy(
            update={
                "gcal_client_id": "cid",
                "gcal_client_secret": "secret",
                "gcal_refresh_token": "refresh",
                "gcal_calendar_id": "parish@group.calendar.google.com",
            }
        )
        monkeypatch.setattr(gcal, "settings", lambda: cfg)

        async def mint_token() -> str:
            return "token"

        async def insert(token: str, payload: dict) -> str:
            if self.insert_error:
                raise gcal.GcalError("insert failed: HTTP 500")
            self.inserted.append(payload)
            return f"g{len(self.inserted)}"

        async def patch(token: str, gid: str, payload: dict) -> None:
            self.patched.append((gid, payload))

        async def delete(token: str, gid: str) -> None:
            self.deleted.append(gid)

        async def list_managed(token: str, time_min: datetime) -> list[dict]:
            return self.listed

        monkeypatch.setattr(gcal, "mint_token", mint_token)
        monkeypatch.setattr(gcal, "insert", insert)
        monkeypatch.setattr(gcal, "patch", patch)
        monkeypatch.setattr(gcal, "delete", delete)
        monkeypatch.setattr(gcal, "list_managed", list_managed)


@pytest.fixture
def fake_gcal(monkeypatch):
    return FakeGcal(monkeypatch)


async def _stored(event_id: int) -> tuple[str | None, str | None]:
    async with db_session() as session:
        event = await session.get(Event, event_id)
        return event.google_event_id, event.google_fingerprint


async def test_unconfigured_sync_is_a_quiet_noop(database, capsys):
    assert await calendar_sync.main() == 0
    assert "not configured" in capsys.readouterr().out


async def test_first_run_pushes_then_second_run_is_idle(database, fake_gcal):
    _, event_id = await _team_and_event()
    assert await calendar_sync.main() == 0
    assert [p["summary"] for p in fake_gcal.inserted] == ["Sunday Mass"]
    payload = fake_gcal.inserted[0]
    assert payload["location"] == "Main church"
    assert payload["extendedProperties"]["private"]["vdb_id"] == str(event_id)
    assert "attendees" not in payload and "description" not in payload
    gid, fp = await _stored(event_id)
    assert gid == "g1" and fp == gcal.fingerprint(payload)

    assert await calendar_sync.main() == 0
    assert len(fake_gcal.inserted) == 1 and not fake_gcal.patched, (
        "an unchanged event costs no API call"
    )


async def test_edit_patches_and_cancel_deletes(database, fake_gcal):
    _, event_id = await _team_and_event()
    await calendar_sync.main()

    async with db_session() as session:
        await event_service.update_event(session, event_id, location="Parish Hall")
    await calendar_sync.main()
    assert [gid for gid, _ in fake_gcal.patched] == ["g1"]
    assert fake_gcal.patched[0][1]["location"] == "Parish Hall"

    async with db_session() as session:
        await event_service.cancel_event(session, event_id, cancelled_by=None)
    await calendar_sync.main()
    assert fake_gcal.deleted == ["g1"]
    assert await _stored(event_id) == (None, None)


async def test_orphaned_calendar_entries_are_collected(database, fake_gcal):
    await _team_and_event()
    fake_gcal.listed = [
        {
            "id": "gone",
            "extendedProperties": {"private": {"vdb_id": "99999", "vdb_managed": "1"}},
        }
    ]
    await calendar_sync.main()
    assert fake_gcal.deleted == ["gone"], (
        "a managed entry with no live event behind it (team CASCADE) is removed"
    )


async def test_live_events_survive_the_gc_sweep(database, fake_gcal):
    _, event_id = await _team_and_event()
    await calendar_sync.main()
    fake_gcal.listed = [
        {
            "id": "g1",
            "extendedProperties": {
                "private": {"vdb_id": str(event_id), "vdb_managed": "1"}
            },
        }
    ]
    await calendar_sync.main()
    assert fake_gcal.deleted == []


async def test_failed_insert_leaves_no_stamp_and_exits_nonzero(database, fake_gcal):
    _, event_id = await _team_and_event()
    fake_gcal.insert_error = True
    assert await calendar_sync.main() == 1
    assert await _stored(event_id) == (None, None), (
        "NULL stamps mean the next run retries the push"
    )
    fake_gcal.insert_error = False
    assert await calendar_sync.main() == 0
    assert len(fake_gcal.inserted) == 1


def test_fingerprint_ignores_key_order():
    a = {"summary": "Mass", "start": {"dateTime": "x", "timeZone": "y"}}
    b = {"start": {"timeZone": "y", "dateTime": "x"}, "summary": "Mass"}
    assert gcal.fingerprint(a) == gcal.fingerprint(b)
    assert gcal.fingerprint(a) != gcal.fingerprint({**a, "summary": "Vespers"})


def test_embed_url_is_config_derived(monkeypatch):
    assert gcal.embed_url() is None  # dev default: unset
    cfg = settings().model_copy(
        update={"gcal_calendar_id": "parish@group.calendar.google.com"}
    )
    monkeypatch.setattr(gcal, "settings", lambda: cfg)
    url = gcal.embed_url()
    assert url.startswith("https://calendar.google.com/calendar/embed?src=")
    assert "parish%40group.calendar.google.com" in url
