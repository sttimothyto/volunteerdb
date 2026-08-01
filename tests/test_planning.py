"""Planning service: vacancy scoping and the proposal lifecycle."""

import pytest
from sqlalchemy.exc import IntegrityError

from volunteerdb.db import db_session
from volunteerdb.models import ProposalStatus, TeamRole
from volunteerdb.permissions import load_actor
from volunteerdb.services import memberships, planning, teams, users, volunteers


async def _parish(session):
    """Liturgy has a leader but no second; Garden has nobody at all."""
    liturgy = await teams.create(session, "Liturgy")
    garden = await teams.create(session, "Garden")
    lena = await volunteers.create(session, "Lena", "Leader")
    vera = await volunteers.create(session, "Vera", "Volunteer")
    await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
    leader_user = await users.create(session, "lena@example.org", volunteer_id=lena.id)
    admin_user = await users.create(session, "admin@example.org", is_admin=True)
    leader_actor = await load_actor(session, leader_user)
    admin_actor = await load_actor(session, admin_user)
    return liturgy, garden, vera, leader_actor, admin_actor, admin_user


async def test_vacancies_scoped_to_managed_teams(database):
    async with db_session() as session:
        liturgy, garden, _, leader_actor, admin_actor, _ = await _parish(session)

        admin_rows = await planning.vacancies(session, admin_actor)
        assert {r.team.id for r in admin_rows} == {liturgy.id, garden.id}
        by_team = {r.team.id: r for r in admin_rows}
        assert not by_team[liturgy.id].missing_leader
        assert by_team[liturgy.id].missing_second
        assert by_team[garden.id].missing_leader and by_team[garden.id].missing_second

        leader_rows = await planning.vacancies(session, leader_actor)
        assert {r.team.id for r in leader_rows} == {liturgy.id}, (
            "leaders see their subtree only"
        )


async def test_propose_duplicate_and_repropose_after_decline(database):
    async with db_session() as session:
        liturgy, _, vera, _, _, admin_user = await _parish(session)
        ids = (liturgy.id, vera.id, admin_user.id)
        p = await planning.propose(
            session,
            team_id=liturgy.id,
            volunteer_id=vera.id,
            role=TeamRole.second,
            proposed_by=admin_user.id,
            note="  reliable  ",
        )
        assert p.status == ProposalStatus.proposed.value
        assert p.note == "reliable"
        first_id = p.id

    liturgy_id, vera_id, admin_id = ids
    with pytest.raises(IntegrityError):
        async with db_session() as session:
            await planning.propose(
                session,
                team_id=liturgy_id,
                volunteer_id=vera_id,
                role=TeamRole.second,
                proposed_by=admin_id,
            )

    async with db_session() as session:
        await planning.decline(session, first_id, decided_by=admin_id)
        again = await planning.propose(
            session,
            team_id=liturgy_id,
            volunteer_id=vera_id,
            role=TeamRole.second,
            proposed_by=admin_id,
        )
        assert again.id != first_id, (
            "the partial unique index only guards OPEN proposals"
        )


async def test_accept_creates_membership_and_flips_status(database):
    async with db_session() as session:
        liturgy, _, vera, _, admin_actor, admin_user = await _parish(session)
        p = await planning.propose(
            session,
            team_id=liturgy.id,
            volunteer_id=vera.id,
            role=TeamRole.second,
            proposed_by=admin_user.id,
        )
        accepted = await planning.accept(session, p.id, decided_by=admin_user.id)
        assert accepted.status == ProposalStatus.accepted.value
        assert accepted.decided_by == admin_user.id and accepted.decided_at is not None

        m = await memberships.find(session, vera.id, liturgy.id)
        assert m is not None and m.role == TeamRole.second

        with pytest.raises(ValueError, match="already accepted"):
            await planning.decline(session, p.id, decided_by=admin_user.id)


async def test_accept_upgrades_an_existing_member(database):
    async with db_session() as session:
        liturgy, _, vera, _, _, admin_user = await _parish(session)
        await memberships.assign(session, vera.id, liturgy.id, TeamRole.member)
        p = await planning.propose(
            session,
            team_id=liturgy.id,
            volunteer_id=vera.id,
            role=TeamRole.second,
            proposed_by=admin_user.id,
        )
        await planning.accept(session, p.id, decided_by=admin_user.id)
        m = await memberships.find(session, vera.id, liturgy.id)
        assert m.role == TeamRole.second, "assign() upserts: the role is upgraded"


async def test_withdraw_and_missing_proposal(database):
    async with db_session() as session:
        liturgy, _, vera, _, _, admin_user = await _parish(session)
        p = await planning.propose(
            session,
            team_id=liturgy.id,
            volunteer_id=vera.id,
            role=TeamRole.leader,
            proposed_by=admin_user.id,
        )
        withdrawn = await planning.withdraw(session, p.id, decided_by=admin_user.id)
        assert withdrawn.status == ProposalStatus.withdrawn.value

        with pytest.raises(LookupError):
            await planning.accept(session, 424242, decided_by=admin_user.id)


async def test_list_proposals_scoped_and_filtered(database):
    async with db_session() as session:
        liturgy, garden, vera, leader_actor, admin_actor, admin_user = await _parish(
            session
        )
        on_liturgy = await planning.propose(
            session,
            team_id=liturgy.id,
            volunteer_id=vera.id,
            role=TeamRole.second,
            proposed_by=admin_user.id,
        )
        await planning.propose(
            session,
            team_id=garden.id,
            volunteer_id=vera.id,
            role=TeamRole.leader,
            proposed_by=admin_user.id,
        )

        admin_views = await planning.list_proposals(session, admin_actor)
        assert len(admin_views) == 2
        assert admin_views[0].proposer_email == "admin@example.org"
        assert admin_views[0].path

        leader_views = await planning.list_proposals(session, leader_actor)
        assert [v.proposal.id for v in leader_views] == [on_liturgy.id], (
            "leaders see proposals for their subtree only"
        )

        await planning.decline(session, on_liturgy.id, decided_by=admin_user.id)
        open_only = await planning.list_proposals(
            session, admin_actor, status=ProposalStatus.proposed.value
        )
        assert [v.proposal.team_id for v in open_only] == [garden.id]
