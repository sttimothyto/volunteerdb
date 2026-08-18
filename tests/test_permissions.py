"""Permission matrix over the fourfold roles + admin, including sub-team cascade."""

from datetime import date

import pytest

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.permissions import load_actor, volunteer_team_ids
from volunteerdb.services import elections, memberships, teams, users, volunteers


@pytest.fixture
async def parish(database):
    """Liturgy > Music; separate Hospitality. One volunteer per role on Liturgy."""
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        music = await teams.create(session, "Music", parent_team_id=liturgy.id)
        hospitality = await teams.create(session, "Hospitality")

        people = {}
        for name, role in [
            ("leader", TeamRole.leader),
            ("second", TeamRole.second),
            ("core", TeamRole.core),
            ("member", TeamRole.member),
        ]:
            v = await volunteers.create(
                session, name.title(), "Person", f"{name}@example.org"
            )
            await memberships.assign(session, v.id, liturgy.id, role)
            people[name] = v

        outsider = await volunteers.create(session, "Out", "Sider", "out@example.org")
        await memberships.assign(session, outsider.id, hospitality.id, TeamRole.member)
        people["outsider"] = outsider

        accounts = {
            name: (
                await users.create(
                    session,
                    f"user-{name}@example.org",
                    volunteer_id=v.id,
                    password="test-pass-phrase",
                )
            )[0]
            for name, v in people.items()
        }
        accounts["admin"], _ = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        ids = {
            "liturgy": liturgy.id,
            "music": music.id,
            "hospitality": hospitality.id,
            "member_vid": people["member"].id,
            "outsider_vid": outsider.id,
        }
    return accounts, ids


async def _actor(accounts, name):
    async with db_session() as session:
        user = await users.get(session, accounts[name].id)
        return await load_actor(session, user)


async def test_leader_and_second_manage_subtree(parish):
    accounts, ids = parish
    for name in ("leader", "second"):
        actor = await _actor(accounts, name)
        assert actor.can_manage_team(ids["liturgy"])
        assert actor.can_manage_team(ids["music"]), "management cascades to sub-teams"
        assert not actor.can_manage_team(ids["hospitality"])
        assert actor.can_view_full_roster(ids["music"])


async def test_core_views_but_does_not_manage(parish):
    accounts, ids = parish
    actor = await _actor(accounts, "core")
    assert not actor.can_manage_team(ids["liturgy"])
    assert actor.can_view_full_roster(ids["liturgy"])
    assert actor.can_view_full_roster(ids["music"]), "core view cascades to sub-teams"
    assert not actor.can_view_roster_names(ids["hospitality"])


async def test_member_sees_names_only_own_team(parish):
    accounts, ids = parish
    actor = await _actor(accounts, "member")
    assert not actor.can_manage_team(ids["liturgy"])
    assert not actor.can_view_full_roster(ids["liturgy"])
    assert actor.can_view_roster_names(ids["liturgy"])
    assert not actor.can_view_roster_names(ids["music"]), "member rights do not cascade"


async def test_contact_edit_rights(parish):
    accounts, ids = parish
    async with db_session() as session:
        member_teams = await volunteer_team_ids(session, ids["member_vid"])
        outsider_teams = await volunteer_team_ids(session, ids["outsider_vid"])

    leader = await _actor(accounts, "leader")
    assert leader.can_edit_volunteer(ids["member_vid"], member_teams)
    assert not leader.can_edit_volunteer(ids["outsider_vid"], outsider_teams)

    member = await _actor(accounts, "member")
    assert member.can_edit_volunteer(ids["member_vid"], member_teams), "self-edit"
    assert not member.can_edit_volunteer(ids["outsider_vid"], outsider_teams)

    admin = await _actor(accounts, "admin")
    assert admin.can_edit_volunteer(ids["outsider_vid"], outsider_teams)
    assert admin.can_manage_team(ids["hospitality"])


async def test_import_export_rights(parish):
    accounts, _ = parish
    for name in ("leader", "second", "admin"):
        actor = await _actor(accounts, name)
        assert actor.can_import_export, f"{name} may import/export"
    for name in ("core", "member", "outsider"):
        actor = await _actor(accounts, name)
        assert not actor.can_import_export, f"{name} may not import/export"


async def test_invite_rights_reach_core_but_stop_at_plain_members(parish):
    """Inviting is the one account-shaped power that is not admin-only. It
    tracks full-roster rights — the people who read the whole roster are the
    people who notice nobody can reach half of it — so core members are in and
    plain members are out, even for a teammate they can see by name."""
    accounts, ids = parish
    async with db_session() as session:
        member_teams = await volunteer_team_ids(session, ids["member_vid"])
        outsider_teams = await volunteer_team_ids(session, ids["outsider_vid"])

    for name in ("leader", "second", "core", "admin"):
        actor = await _actor(accounts, name)
        assert actor.can_invite_volunteer(member_teams), f"{name} may invite"

    member = await _actor(accounts, "member")
    assert member.can_view_roster_names(ids["liturgy"]), "sees the name..."
    assert not member.can_invite_volunteer(member_teams), "...but cannot invite"

    outsider = await _actor(accounts, "outsider")
    assert not outsider.can_invite_volunteer(member_teams)

    for name in ("leader", "second", "core"):
        actor = await _actor(accounts, name)
        assert not actor.can_invite_volunteer(outsider_teams), (
            f"{name} may not invite another ministry's people"
        )
    admin = await _actor(accounts, "admin")
    assert admin.can_invite_volunteer(outsider_teams)

    # the case the parish actually has: Liturgy's people run its sub-teams
    async with db_session() as session:
        singer = await volunteers.create(
            session, "Singer", "Person", "sing@example.org"
        )
        await memberships.assign(session, singer.id, ids["music"], TeamRole.member)
        music_teams = await volunteer_team_ids(session, singer.id)
    for name in ("leader", "second", "core"):
        actor = await _actor(accounts, name)
        assert actor.can_invite_volunteer(music_teams), (
            f"{name} of Liturgy reaches a Music member — rights cascade"
        )
    assert not member.can_invite_volunteer(music_teams), "member rights do not cascade"


async def test_only_an_admin_invites_someone_on_no_team(parish):
    """A volunteer on no team has no leader answerable for them."""
    accounts, _ = parish
    for name in ("leader", "second", "core", "member", "outsider"):
        actor = await _actor(accounts, name)
        assert not actor.can_invite_volunteer(set())
    admin = await _actor(accounts, "admin")
    assert admin.can_invite_volunteer(set())


async def test_workload_view_rights(parish):
    accounts, ids = parish
    async with db_session() as session:
        member_teams = await volunteer_team_ids(session, ids["member_vid"])
        outsider_teams = await volunteer_team_ids(session, ids["outsider_vid"])

    for name in ("leader", "second"):
        actor = await _actor(accounts, name)
        assert actor.can_view_workload(member_teams), (
            f"{name} sees their people's workload"
        )
        assert not actor.can_view_workload(outsider_teams), (
            "not other ministries' people"
        )

    core = await _actor(accounts, "core")
    assert not core.can_view_workload(member_teams), "core members never see workload"

    member = await _actor(accounts, "member")
    assert not member.can_view_workload(member_teams), "not even one's own workload"

    admin = await _actor(accounts, "admin")
    assert admin.can_view_workload(member_teams)
    assert admin.can_view_workload(outsider_teams)


async def test_elections_access_without_any_roll(parish):
    accounts, _ = parish
    for name in ("leader", "second", "admin"):
        assert (await _actor(accounts, name)).can_access_elections, name
    for name in ("core", "member", "outsider"):
        assert not (await _actor(accounts, name)).can_access_elections, name


async def test_voting_roll_grants_elections_access(parish):
    accounts, ids = parish
    async with db_session() as session:
        proposal = await elections.create_proposal(
            session,
            team_id=ids["liturgy"],
            role=TeamRole.leader,
            nomination_deadline=date(2026, 8, 15),
            voting_deadline=date(2026, 8, 25),
            created_by=accounts["admin"].id,
            candidates=[elections.CandidateInput(ids["outsider_vid"])],
            today=date(2026, 8, 10),
        )
        pid, team_id = proposal.id, proposal.team_id

    core = await _actor(accounts, "core")  # on the template roll
    assert core.voter_proposal_ids == frozenset({pid})
    assert core.can_access_elections
    assert core.can_view_proposal(pid, team_id)
    assert not core.can_manage_team(team_id)

    member = await _actor(accounts, "member")  # not on the roll
    assert not member.voter_proposal_ids
    assert not member.can_access_elections
    assert not member.can_view_proposal(pid, team_id)

    leader = await _actor(accounts, "leader")
    assert leader.can_view_proposal(pid, team_id), "managers view via the team"
