"""Membership timeline: spells stitched from live + history rows.

Spell boundaries are system times (rev 0011 dropped the operator-entered
joined_on): a spell starts when its membership record was created and ends
when it was deleted.
"""

from datetime import date

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, volunteers


async def _fixtures(session, team_name="Choir"):
    v = await volunteers.create(session, "Tim", "Traveller")
    t = await teams.create(session, team_name)
    return v.id, t.id


async def test_ongoing_membership_is_one_open_spell(database):
    async with db_session(user_id=1) as session:
        vid, tid = await _fixtures(session)
        await memberships.assign(session, vid, tid, TeamRole.member)

    async with db_session() as session:
        spells = await volunteers.timeline(session, vid)
    assert len(spells) == 1
    spell = spells[0]
    assert (
        spell.team_id == tid and spell.team_name == "Choir" and not spell.team_deleted
    )
    assert spell.role == TeamRole.member
    assert spell.start == date.today()
    assert spell.end is None
    assert len(spell.segments) == 1
    assert spell.segments[0].role == TeamRole.member and spell.segments[0].end is None


async def test_role_change_merges_into_one_spell(database):
    async with db_session(user_id=1) as session:
        vid, tid = await _fixtures(session)
        await memberships.assign(session, vid, tid, TeamRole.member)
    async with db_session(user_id=1) as session:
        await memberships.assign(session, vid, tid, TeamRole.leader)

    async with db_session() as session:
        (spell,) = await volunteers.timeline(session, vid)
    assert spell.role == TeamRole.leader
    assert spell.end is None
    assert [s.role for s in spell.segments] == [TeamRole.member, TeamRole.leader]
    assert spell.segments[0].end == spell.segments[1].start  # abut exactly
    assert spell.segments[1].end is None


async def test_leave_then_rejoin_splits_spells(database):
    async with db_session(user_id=1) as session:
        vid, tid = await _fixtures(session)
        m = await memberships.assign(session, vid, tid, TeamRole.member)
        mid = m.id
    async with db_session(user_id=1) as session:
        await memberships.remove(session, mid)
    async with db_session(user_id=1) as session:
        await memberships.assign(session, vid, tid, TeamRole.core)

    async with db_session() as session:
        spells = await volunteers.timeline(session, vid)
    assert len(spells) == 2
    first, second = spells
    assert first.team_id == second.team_id == tid
    assert first.end == date.today() and first.role == TeamRole.member
    assert second.end is None and second.role == TeamRole.core


async def test_spells_on_two_teams_stay_independent_and_sort_by_start(database):
    """No other timeline test spans more than one team, so the run-splitting on
    team_id and the final sort are otherwise unexercised."""
    async with db_session(user_id=1) as session:
        vid, choir = await _fixtures(session)
        ushers = await teams.create(session, "Ushers")
        ushers_id = ushers.id
        await memberships.assign(session, vid, choir, TeamRole.member)
        await memberships.assign(session, vid, ushers_id, TeamRole.core)
    async with db_session(user_id=1) as session:
        await memberships.assign(session, vid, choir, TeamRole.leader)

    async with db_session() as session:
        spells = await volunteers.timeline(session, vid)

    # same start date (both created today) → the name breaks the sorting tie
    assert [(s.team_name, s.start) for s in spells] == [
        ("Choir", date.today()),
        ("Ushers", date.today()),
    ], "one spell per team"
    choir_spell, ushers_spell = spells
    assert choir_spell.role == TeamRole.leader
    assert [s.role for s in choir_spell.segments] == [TeamRole.member, TeamRole.leader]
    assert ushers_spell.role == TeamRole.core
    assert len(ushers_spell.segments) == 1, (
        "the Choir role change must not leak across teams"
    )


async def test_deleted_team_uses_last_historical_name(database):
    async with db_session(user_id=1) as session:
        vid, tid = await _fixtures(session)
        await memberships.assign(session, vid, tid, TeamRole.member)
    async with db_session(user_id=1) as session:
        await teams.delete(session, tid)

    async with db_session() as session:
        (spell,) = await volunteers.timeline(session, vid)
    assert spell.team_name == "Choir"
    assert spell.team_deleted
    assert spell.end == date.today()
