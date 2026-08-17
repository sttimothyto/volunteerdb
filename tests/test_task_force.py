"""Task forces: creation as a child of the owner, roster union with the
highest-role rule, event repointing, sign-up by collaborator members, the
teardown ordering that must not cascade the event away, as-of history
visibility, and the teams.delete guard."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.db import db_session
from volunteerdb.jobs import task_force_cleanup
from volunteerdb.models import Event, TeamRole
from volunteerdb.services import events as event_service
from volunteerdb.services import memberships, task_force, teams, volunteers

TZ = ZoneInfo("America/Toronto")


def _at(days_ahead: int, hour: int) -> datetime:
    return datetime.combine(date.today() + timedelta(days=days_ahead), time(hour), TZ)


async def _parish() -> dict:
    """Liturgy (Lena leads, Mia member) and Choir (Carl leads, Oda member;
    Mia sings in the choir too — the dedupe case)."""
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        choir = await teams.create(session, "Choir")
        ids = {"liturgy": liturgy.id, "choir": choir.id}
        for key, first, team_id, role in (
            ("lena", "Lena", liturgy.id, TeamRole.leader),
            ("mia", "Mia", liturgy.id, TeamRole.member),
            ("carl", "Carl", choir.id, TeamRole.leader),
            ("oda", "Oda", choir.id, TeamRole.member),
        ):
            v = await volunteers.create(
                session, first, "Volunteer", f"{key}@example.org"
            )
            await memberships.assign(session, v.id, team_id, role)
            ids[key] = v.id
        # Mia is also a Choir core member: the union must keep her ONE row
        # in the task force, at her strongest role
        await memberships.assign(session, ids["mia"], choir.id, TeamRole.core)
        return ids


async def _event(team_id: int, *, days_ahead: int = 7) -> int:
    async with db_session() as session:
        created = await event_service.create_event(
            session,
            team_id=team_id,
            title="Parish Picnic",
            starts_at=_at(days_ahead, 10),
            ends_at=_at(days_ahead, 14),
            location="Church grounds",
            created_by=None,
        )
        return created[0].id


async def test_collaboration_builds_the_task_force(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session, event_id=event_id, source_team_id=ids["choir"], created_by=None
        )
        assert meta.parent_team_id == ids["liturgy"], (
            "child of the owner: its leaders manage via the subtree cascade"
        )
        assert "task force" in meta.name and "[Auto]" in (meta.description or "")

        event = await session.get(Event, event_id)
        assert event.team_id == meta.id, "the event now belongs to the task force"

        view = await task_force.get_for_event(session, event_id)
        assert {t.id for t in view.sources} == {ids["liturgy"], ids["choir"]}

        roster = await teams.roster(session, meta.id)
        by_vid = {v.id: m.role for m, v in roster}
        assert by_vid == {
            ids["lena"]: TeamRole.leader,
            ids["carl"]: TeamRole.leader,  # collaborating leaders co-manage
            ids["mia"]: TeamRole.core,  # her strongest role across sources
            ids["oda"]: TeamRole.member,
        }


async def test_collaborator_members_can_sign_up(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        detail = await event_service.detail(session, event_id)
        with pytest.raises(ValueError, match="only members"):
            await event_service.sign_up(
                session,
                slot_id=detail.slots[0].slot.id,
                volunteer_id=ids["oda"],
            )
    async with db_session() as session:
        await task_force.add_collaborating_team(
            session, event_id=event_id, source_team_id=ids["choir"], created_by=None
        )
        detail = await event_service.detail(session, event_id)
        a = await event_service.sign_up(
            session, slot_id=detail.slots[0].slot.id, volunteer_id=ids["oda"]
        )
        assert a.volunteer_id == ids["oda"]


async def test_duplicate_and_self_sources_are_refused(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        with pytest.raises(ValueError, match="already staffs"):
            await task_force.add_collaborating_team(
                session,
                event_id=event_id,
                source_team_id=ids["liturgy"],
                created_by=None,
            )
        await task_force.add_collaborating_team(
            session, event_id=event_id, source_team_id=ids["choir"], created_by=None
        )
        with pytest.raises(ValueError, match="already staffs"):
            await task_force.add_collaborating_team(
                session,
                event_id=event_id,
                source_team_id=ids["choir"],
                created_by=None,
            )


async def test_refresh_picks_up_source_drift_without_downgrades(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session, event_id=event_id, source_team_id=ids["choir"], created_by=None
        )
        meta_id = meta.id
        # a newcomer joins Choir after the copy; Oda gets promoted INSIDE
        # the task force (a per-event decision the refresh must not undo)
        newbie = await volunteers.create(session, "Nina", "New", "nina@example.org")
        await memberships.assign(session, newbie.id, ids["choir"], TeamRole.member)
        await memberships.assign(session, ids["oda"], meta_id, TeamRole.core)
        newbie_id = newbie.id
    async with db_session() as session:
        added = await task_force.refresh_rosters(session, event_id)
        assert added == 1
        roster = await teams.roster(session, meta_id)
        by_vid = {v.id: m.role for m, v in roster}
        assert by_vid[newbie_id] == TeamRole.member
        assert by_vid[ids["oda"]] == TeamRole.core, "never downgraded"


async def test_teardown_restores_the_event_and_keeps_history(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session, event_id=event_id, source_team_id=ids["choir"], created_by=None
        )
        meta_id = meta.id
        detail = await event_service.detail(session, event_id)
        await event_service.sign_up(
            session, slot_id=detail.slots[0].slot.id, volunteer_id=ids["oda"]
        )
        before_teardown = datetime.now(TZ)
    # push the event into the past so the cleanup job considers it due
    async with db_session() as session:
        past = _at(-3, 9)
        await event_service.update_event(
            session, event_id, starts_at=past, ends_at=past + timedelta(hours=2)
        )
    assert await task_force_cleanup.main() == 0

    async with db_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None, "the repoint-then-flush order kept the event"
        assert event.team_id == ids["liturgy"], "ownership restored"
        assert await teams.get(session, meta_id) is None, "the meta team is gone"
        assert await task_force.get_for_event(session, event_id) is None

        detail = await event_service.detail(session, event_id)
        assert [v.id for sv in detail.slots for _, v in sv.entries] == [ids["oda"]], (
            "the attendance record survived the teardown"
        )
        # "visible in history": the as-of view still shows the team
        assert await teams.get(session, meta_id, at=before_teardown) is not None


async def test_cancelled_events_tear_down_too(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        await task_force.add_collaborating_team(
            session, event_id=event_id, source_team_id=ids["choir"], created_by=None
        )
        await event_service.cancel_event(session, event_id, cancelled_by=None)
    assert await task_force_cleanup.main() == 0
    async with db_session() as session:
        assert await task_force.get_for_event(session, event_id) is None
        event = await session.get(Event, event_id)
        assert event.team_id == ids["liturgy"]


async def test_live_task_force_team_cannot_be_deleted_directly(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session, event_id=event_id, source_team_id=ids["choir"], created_by=None
        )
        with pytest.raises(ValueError, match="task force"):
            await teams.delete(session, meta.id)


async def test_adding_to_a_finished_event_is_refused(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        past = _at(-3, 9)
        await event_service.update_event(
            session, event_id, starts_at=past, ends_at=past + timedelta(hours=2)
        )
        with pytest.raises(ValueError, match="already ended"):
            await task_force.add_collaborating_team(
                session,
                event_id=event_id,
                source_team_id=ids["choir"],
                created_by=None,
            )
