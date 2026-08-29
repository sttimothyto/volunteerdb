"""Task forces: creation as a child of the owner, roster union with the
highest-role rule, event repointing, sign-up by collaborator members, the
teardown ordering that must not cascade the event away, as-of history
visibility, and the teams.delete guard."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb import errors
from volunteerdb.actors import load_actor
from volunteerdb.db import db_session
from volunteerdb.jobs import task_force_cleanup
from volunteerdb.models import Event, TeamRole
from volunteerdb.permissions import volunteer_team_ids
from volunteerdb.services import events as event_service
from volunteerdb.services import memberships, task_force, teams, users, volunteers

from tests import mint
from tests.fp_helpers import ok, refused

TZ = ZoneInfo("America/Toronto")


def _at(days_ahead: int, hour: int) -> datetime:
    return datetime.combine(date.today() + timedelta(days=days_ahead), time(hour), TZ)


async def _parish() -> dict:
    """Liturgy (Lena leads, Mia member) and Choir (Carl leads, Oda member;
    Mia sings in the choir too — the dedupe case)."""
    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        choir = ok(await teams.create(session, None, "Choir"))
        ids = {"liturgy": liturgy.id, "choir": choir.id}
        for key, first, team_id, role in (
            ("lena", "Lena", liturgy.id, TeamRole.leader),
            ("mia", "Mia", liturgy.id, TeamRole.member),
            ("carl", "Carl", choir.id, TeamRole.leader),
            ("oda", "Oda", choir.id, TeamRole.member),
        ):
            v = ok(
                await volunteers.create(
                    session, None, first, "Volunteer", f"{key}@example.org"
                )
            )
            ok(await memberships.assign(session, None, v.id, team_id, role))
            ids[key] = v.id
        # Mia is also a Choir core member: the union must keep her ONE row
        # in the task force, at her strongest role
        ok(await memberships.assign(session, None, ids["mia"], choir.id, TeamRole.core))
        return ids


async def _event(team_id: int, *, days_ahead: int = 7) -> int:
    async with db_session() as session:
        created = ok(
            await event_service.create_event(
                session,
                None,
                team_id=team_id,
                title="Parish Picnic",
                starts_at=_at(days_ahead, 10),
                ends_at=_at(days_ahead, 14),
                location="Church grounds",
                created_by=None,
                tz=mint.tz(),
                series_id=mint.uuid(),
            )
        )
        return created[0].id


async def test_collaboration_builds_the_task_force(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        assert meta.parent_team_id == ids["liturgy"], (
            "child of the owner: its leaders manage via the subtree cascade"
        )
        assert "task force" in meta.name and "[Auto]" in (meta.description or "")

        event = await session.get(Event, event_id)
        assert event.team_id == meta.id, "the event now belongs to the task force"

        view = await task_force.get_for_event(session, event_id)
        assert {t.id for t in view.sources} == {ids["liturgy"], ids["choir"]}

        roster = ok(await teams.roster(session, None, meta.id))
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
        detail = ok(await event_service.detail(session, None, event_id))
        refused(
            await event_service.sign_up(
                session,
                None,
                slot_id=detail.slots[0].slot.id,
                volunteer_id=ids["oda"],
                now=mint.now(),
            ),
            errors.Invalid,
            match="only members",
        )
    async with db_session() as session:
        await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        detail = ok(await event_service.detail(session, None, event_id))
        a = ok(
            await event_service.sign_up(
                session,
                None,
                slot_id=detail.slots[0].slot.id,
                volunteer_id=ids["oda"],
                now=mint.now(),
            )
        )
        assert a.volunteer_id == ids["oda"]


async def test_duplicate_and_self_sources_are_refused(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        with pytest.raises(ValueError, match="already staffs"):
            await task_force.add_collaborating_team(
                session,
                None,
                event_id=event_id,
                source_team_id=ids["liturgy"],
                created_by=None,
            )
        await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        with pytest.raises(ValueError, match="already staffs"):
            await task_force.add_collaborating_team(
                session,
                None,
                event_id=event_id,
                source_team_id=ids["choir"],
                created_by=None,
            )


async def test_refresh_picks_up_source_drift_without_downgrades(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        meta_id = meta.id
        # a newcomer joins Choir after the copy; Oda gets promoted INSIDE
        # the task force (a per-event decision the refresh must not undo)
        newbie = ok(
            await volunteers.create(session, None, "Nina", "New", "nina@example.org")
        )
        ok(
            await memberships.assign(
                session, None, newbie.id, ids["choir"], TeamRole.member
            )
        )
        ok(await memberships.assign(session, None, ids["oda"], meta_id, TeamRole.core))
        newbie_id = newbie.id
    async with db_session() as session:
        added = await task_force.refresh_rosters(session, None, event_id)
        assert added == 1
        roster = ok(await teams.roster(session, None, meta_id))
        by_vid = {v.id: m.role for m, v in roster}
        assert by_vid[newbie_id] == TeamRole.member
        assert by_vid[ids["oda"]] == TeamRole.core, "never downgraded"


async def test_teardown_restores_the_event_and_keeps_history(database, env):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        meta_id = meta.id
        detail = ok(await event_service.detail(session, None, event_id))
        ok(
            await event_service.sign_up(
                session,
                None,
                slot_id=detail.slots[0].slot.id,
                volunteer_id=ids["oda"],
                now=mint.now(),
            )
        )
        before_teardown = datetime.now(TZ)
    # push the event into the past so the cleanup job considers it due
    async with db_session() as session:
        past = _at(-3, 9)
        ok(
            await event_service.update_event(
                session,
                None,
                event_id,
                starts_at=past,
                ends_at=past + timedelta(hours=2),
            )
        )
    assert await task_force_cleanup.main(env) == 0

    async with db_session() as session:
        event = await session.get(Event, event_id)
        assert event is not None, "the repoint-then-flush order kept the event"
        assert event.team_id == ids["liturgy"], "ownership restored"
        assert await teams.get(session, meta_id) is None, "the meta team is gone"
        assert await task_force.get_for_event(session, event_id) is None

        detail = ok(await event_service.detail(session, None, event_id))
        assert [v.id for sv in detail.slots for _, v in sv.entries] == [ids["oda"]], (
            "the attendance record survived the teardown"
        )
        # "visible in history": the as-of view still shows the team
        assert await teams.get(session, meta_id, at=before_teardown) is not None


async def test_cancelled_events_tear_down_too(database, env):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        ok(
            await event_service.cancel_event(
                session, None, event_id, cancelled_by=None, now=mint.now()
            )
        )
    assert await task_force_cleanup.main(env) == 0
    async with db_session() as session:
        assert await task_force.get_for_event(session, event_id) is None
        event = await session.get(Event, event_id)
        assert event.team_id == ids["liturgy"]


async def test_live_task_force_team_cannot_be_deleted_directly(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        refused(
            await teams.delete(session, None, meta.id),
            errors.Invalid,
            match="task force",
        )


async def test_adding_to_a_finished_event_is_refused(database):
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        past = _at(-3, 9)
        ok(
            await event_service.update_event(
                session,
                None,
                event_id,
                starts_at=past,
                ends_at=past + timedelta(hours=2),
            )
        )
        with pytest.raises(ValueError, match="already ended"):
            await task_force.add_collaborating_team(
                session,
                None,
                event_id=event_id,
                source_team_id=ids["choir"],
                created_by=None,
            )


async def test_a_task_force_lends_a_roster_it_does_not_hand_over_its_people(database):
    """A collaborating team's members must not become the host's to read and edit.

    The meta team is a child of the owner, so before the people/affairs split
    it landed in `managed_team_ids` and every borrowed member suddenly shared a
    managed team with whoever set the collaboration up — contact details,
    notes, workload and invite links included. Adding a collaborator is a
    unilateral act (the picker offers every active team), so that was a
    parish-wide read of anyone's private data for any team leader.
    """
    ids = await _parish()
    event_id = await _event(ids["liturgy"])
    async with db_session() as session:
        meta = await task_force.add_collaborating_team(
            session,
            None,
            event_id=event_id,
            source_team_id=ids["choir"],
            created_by=None,
        )
        lena, _ = ok(
            await users.create(
                session,
                "lena@example.org",
                volunteer_id=ids["lena"],
                invite=mint.fresh_invite(),
            )
        )
        actor = await load_actor(session, lena)
        oda_teams = await volunteer_team_ids(session, ids["oda"])

        # affairs: unchanged, or the event stops being manageable
        assert actor.can_manage_team(meta.id), "the host still runs the event"
        assert actor.can_view_roster_names(meta.id), "and still sees who is staffing"

        # people: the borrowed roster stays the choir's
        assert not actor.can_view_full_roster(meta.id), (
            "no contact details through the meta roster — the same leak by "
            "another door if this is ever allowed"
        )
        assert meta.id not in actor.people_team_ids
        assert not actor.can_view_volunteer(ids["oda"], oda_teams)
        assert not actor.can_edit_volunteer(ids["oda"], oda_teams)
        assert not actor.can_view_workload(oda_teams)
        assert not actor.can_invite_volunteer(oda_teams)

        # ...while her own member is still hers
        mia_teams = await volunteer_team_ids(session, ids["mia"])
        assert actor.can_edit_volunteer(ids["mia"], mia_teams)
