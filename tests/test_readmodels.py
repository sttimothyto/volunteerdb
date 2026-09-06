"""The read models: what a detail page shows, read once (services/readmodels.py).

Each answers two questions -- the rows, and which sections exist for this
reader -- so every test seeds one parish and asks the same read model the
same question as three readers: the team's leader, one of its members, and
a volunteer on another team.
"""

from datetime import datetime, time, timedelta

from volunteerdb.actors import load_actor
from volunteerdb.errors import Forbidden, NotFound
from volunteerdb.fp import Err
from volunteerdb.models import TeamRole
from volunteerdb.services import (
    elections,
    events,
    memberships,
    readmodels,
    teams,
    users,
    volunteers,
)

from tests import mint
from tests.conftest import db_session
from tests.fp_helpers import ok

TZ = mint.tz()


async def _parish(session) -> dict[str, int]:
    """Liturgy (Lena leads, Mia member) and Choir (Oda member), each with an
    account."""
    liturgy = ok(await teams.create(session, None, "Liturgy"))
    choir = ok(await teams.create(session, None, "Choir"))
    people = {}
    for key, first, last, team, role in (
        ("lena", "Lena", "Leader", liturgy, TeamRole.leader),
        ("mia", "Mia", "Member", liturgy, TeamRole.member),
        ("oda", "Oda", "Chorister", choir, TeamRole.member),
    ):
        email = f"{key}@example.org"
        v = ok(await volunteers.create(session, None, first, last, email))
        ok(await memberships.assign(session, None, v.id, team.id, role))
        u, _ = ok(
            await users.create(
                session, email, volunteer_id=v.id, invite=mint.fresh_invite()
            )
        )
        people[key], people[f"{key}_u"] = v.id, u.id
    return {"liturgy": liturgy.id, "choir": choir.id, **people}


async def _actor(session, user_id: int):
    return await load_actor(session, await users.get(session, user_id))


def _at(days: int, hour: int) -> datetime:
    return datetime.combine(mint.today() + timedelta(days=days), time(hour), TZ)


async def _seed_event(session, team_id: int) -> int:
    created = ok(
        await events.create_event(
            session,
            None,
            team_id=team_id,
            title="Sunday Mass",
            starts_at=_at(7, 10),
            ends_at=_at(7, 12),
            slots=[events.SlotInput("Lector", 2)],
            created_by=None,
            tz=TZ,
            series_id=mint.uuid(),
        )
    )
    return created[0].id


# --- one event -----------------------------------------------------------------


async def test_event_workroom_tells_each_reader_what_they_may_do(database):
    async with db_session() as session:
        ids = await _parish(session)
        event_id = await _seed_event(session, ids["liturgy"])
    async with db_session() as session:
        lena = await _actor(session, ids["lena_u"])
        room = ok(
            await readmodels.event_workroom(session, lena, event_id, now=mint.now())
        )
        assert room.can_manage and room.am_member and room.upcoming
        assert room.attendance is None, "the sheet is for after the event"
        assert [v.full_name for _, v in room.roster] == ["Lena Leader", "Mia Member"]
        assert set(room.picker_options()) == {ids["lena"], ids["mia"]}
        assert ids["choir"] in room.collaborator_options
        assert ids["liturgy"] not in room.collaborator_options, "already staffing"

        mia = await _actor(session, ids["mia_u"])
        room = ok(
            await readmodels.event_workroom(session, mia, event_id, now=mint.now())
        )
        assert room.am_member and not room.can_manage
        assert room.roster == [] and room.picker_options() == {}
        assert room.collaborator_options == {}

        oda = await _actor(session, ids["oda_u"])
        refused = await readmodels.event_workroom(
            session, oda, event_id, now=mint.now()
        )
        assert isinstance(refused, Err) and isinstance(refused.error, Forbidden)
        missing = await readmodels.event_workroom(
            session, lena, 999_999, now=mint.now()
        )
        assert isinstance(missing, Err) and isinstance(missing.error, NotFound)


async def test_event_workroom_follows_the_assignee_and_the_open_call(database):
    async with db_session() as session:
        ids = await _parish(session)
        event_id = await _seed_event(session, ids["liturgy"])
    async with db_session() as session:
        mia = await _actor(session, ids["mia_u"])
        room = ok(
            await readmodels.event_workroom(session, mia, event_id, now=mint.now())
        )
        ok(
            await events.sign_up(
                session,
                mia,
                slot_id=room.view.slots[0].slot.id,
                volunteer_id=ids["mia"],
                now=mint.now(),
            )
        )
    async with db_session() as session:
        mia = await _actor(session, ids["mia_u"])
        room = ok(
            await readmodels.event_workroom(session, mia, event_id, now=mint.now())
        )
        assert room.my_assignment is not None
        assert room.roster, "an assignee gets the roster for the hand-off picker"
        assert ids["mia"] not in room.picker_options(), "already on a slot"
        ok(
            await events.request_sub(
                session,
                mia,
                assignment_id=room.my_assignment.id,
                requested_by=ids["mia_u"],
                note="",
                now=mint.now(),
            )
        )
    async with db_session() as session:
        mia = await _actor(session, ids["mia_u"])
        room = ok(
            await readmodels.event_workroom(session, mia, event_id, now=mint.now())
        )
        assert room.my_assignment.id in room.sub_wanted
        assert room.claimable_subs == [], "never your own call"
        lena = await _actor(session, ids["lena_u"])
        room = ok(
            await readmodels.event_workroom(session, lena, event_id, now=mint.now())
        )
        assert [a.volunteer_id for _, a in room.claimable_subs] == [ids["mia"]]


async def test_event_workroom_hands_the_manager_the_sheet_after_the_event(database):
    async with db_session() as session:
        ids = await _parish(session)
        event_id = await _seed_event(session, ids["liturgy"])
        view = ok(await events.detail(session, None, event_id))
        ok(
            await events.sign_up(
                session,
                None,
                slot_id=view.slots[0].slot.id,
                volunteer_id=ids["mia"],
                now=mint.now(),
            )
        )
        ok(
            await events.update_event(
                session, None, event_id, starts_at=_at(-2, 9), ends_at=_at(-2, 11)
            )
        )
    async with db_session() as session:
        lena = await _actor(session, ids["lena_u"])
        room = ok(
            await readmodels.event_workroom(session, lena, event_id, now=mint.now())
        )
        assert not room.upcoming
        assert [v.full_name for _, _, v in room.attendance] == ["Mia Member"]
        mia = await _actor(session, ids["mia_u"])
        room = ok(
            await readmodels.event_workroom(session, mia, event_id, now=mint.now())
        )
        assert room.attendance is None, "the sheet is the manager's"


# --- one team ------------------------------------------------------------------


async def test_team_room_grades_the_roster_by_the_readers_rights(database):
    async with db_session() as session:
        ids = await _parish(session)
    async with db_session() as session:
        lena = await _actor(session, ids["lena_u"])
        room = ok(
            await readmodels.team_room(
                session, lena, ids["liturgy"], now=mint.now(), tz=TZ
            )
        )
        assert room.live and room.can_manage and room.can_full and room.can_invite
        assert room.path == "Liturgy" and room.children == []
        assert room.emails == ["lena@example.org", "mia@example.org"]
        assert room.volunteer_options, "the add-member picker has names"
        assert set(room.accounts) == {ids["lena"], ids["mia"]}

        mia = await _actor(session, ids["mia_u"])
        room = ok(
            await readmodels.team_room(
                session, mia, ids["liturgy"], now=mint.now(), tz=TZ
            )
        )
        assert room.can_names and not room.can_full and not room.can_manage
        assert [v.full_name for _, v in room.roster] == ["Lena Leader", "Mia Member"]
        assert room.volunteer_options == {} and room.sheet is None

        oda = await _actor(session, ids["oda_u"])
        room = ok(
            await readmodels.team_room(
                session, oda, ids["liturgy"], now=mint.now(), tz=TZ
            )
        )
        assert not room.can_names and room.roster == [] and room.accounts == {}

        missing = await readmodels.team_room(
            session, lena, 999_999, now=mint.now(), tz=TZ
        )
        assert isinstance(missing, Err) and isinstance(missing.error, NotFound)


async def test_team_room_is_read_only_on_a_snapshot(database):
    async with db_session() as session:
        ids = await _parish(session)
    async with db_session() as session:
        lena = await _actor(session, ids["lena_u"])
        room = ok(
            await readmodels.team_room(
                session, lena, ids["liturgy"], now=mint.now(), tz=TZ, at=mint.now()
            )
        )
        assert not room.live
        assert room.can_full and not room.can_manage and not room.can_invite
        assert [v.full_name for _, v in room.roster] == ["Lena Leader", "Mia Member"]
        assert room.upcoming_events == [] and room.page is None


# --- one volunteer -------------------------------------------------------------


async def test_volunteer_profile_tiers_by_the_readers_rights(database):
    async with db_session() as session:
        ids = await _parish(session)
    async with db_session() as session:
        lena = await _actor(session, ids["lena_u"])
        profile = ok(
            await readmodels.volunteer_profile(
                session, lena, ids["mia"], now=mint.now(), tz=TZ
            )
        )
        assert profile.can_view and profile.can_edit
        assert profile.team_ids == {ids["liturgy"]}
        assert profile.hours is not None and profile.workload is not None
        assert profile.assignable == {ids["liturgy"]: "Liturgy"}
        assert [t.name for _, t in profile.assignments] == ["Liturgy"]
        assert (
            profile.account is not None and profile.account.volunteer_id == ids["mia"]
        )

        oda = await _actor(session, ids["oda_u"])
        profile = ok(
            await readmodels.volunteer_profile(
                session, oda, ids["mia"], now=mint.now(), tz=TZ
            )
        )
        assert not profile.can_view and not profile.can_edit
        assert profile.hours is None and profile.impact == []
        assert profile.workload is None and profile.assignable == {}

        missing = await readmodels.volunteer_profile(
            session, lena, 999_999, now=mint.now(), tz=TZ
        )
        assert isinstance(missing, Err) and isinstance(missing.error, NotFound)


# --- one proposal --------------------------------------------------------------


async def test_proposal_workroom_knows_the_manager_from_the_voter(database):
    today = mint.today()
    async with db_session() as session:
        ids = await _parish(session)
        # nominations closed yesterday, so the proposal is in its voting phase
        proposal = ok(
            await elections.create_proposal(
                session,
                None,
                team_id=ids["liturgy"],
                role=TeamRole.second,
                nomination_deadline=today - timedelta(days=1),
                voting_deadline=today + timedelta(days=7),
                created_by=ids["lena_u"],
                candidates=[elections.CandidateInput(ids["mia"], "steady hands")],
                today=today - timedelta(days=1),
            )
        )
    async with db_session() as session:
        lena = await _actor(session, ids["lena_u"])
        room = ok(
            await readmodels.proposal_workroom(
                session, lena, proposal.id, now=mint.now(), tz=TZ
            )
        )
        assert room.can_manage and room.is_voter, (
            "the leader runs the seat AND is on its roll"
        )
        assert room.phase is elections.ProposalPhase.voting
        assert room.my_scores == {}
        assert list(room.names.values()) == ["Mia Member"]
        assert room.volunteer_options, "the pickers have names"
        (candidate_id,) = room.names
        ok(
            await elections.cast_ballot(
                session,
                lena,
                proposal.id,
                voter_volunteer_id=ids["lena"],
                scores={candidate_id: 4},
                today=today,
                now=mint.now(),
            )
        )
    async with db_session() as session:
        lena = await _actor(session, ids["lena_u"])
        room = ok(
            await readmodels.proposal_workroom(
                session, lena, proposal.id, now=mint.now(), tz=TZ
            )
        )
        assert room.my_scores == {candidate_id: 4}, (
            "a ballot is not secret to its owner"
        )

        mia = await _actor(session, ids["mia_u"])
        refused = await readmodels.proposal_workroom(
            session, mia, proposal.id, now=mint.now(), tz=TZ
        )
        assert isinstance(refused, Err) and isinstance(refused.error, Forbidden), (
            "a candidate who is neither a manager nor on the roll"
        )
