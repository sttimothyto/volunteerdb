"""Permission matrix over the fourfold roles + admin, including sub-team cascade."""

import pytest

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.permissions import load_actor, volunteer_team_ids
from volunteerdb.services import memberships, teams, users, volunteers


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
            v = await volunteers.create(session, name.title(), "Person", f"{name}@example.org")
            await memberships.assign(session, v.id, liturgy.id, role)
            people[name] = v

        outsider = await volunteers.create(session, "Out", "Sider", "out@example.org")
        await memberships.assign(session, outsider.id, hospitality.id, TeamRole.member)
        people["outsider"] = outsider

        accounts = {
            name: await users.create(
                session, f"user-{name}@example.org", volunteer_id=v.id, password="pw"
            )
            for name, v in people.items()
        }
        accounts["admin"] = await users.create(
            session, "admin@example.org", is_admin=True, password="pw"
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


async def test_capacity_view_rights(parish):
    accounts, ids = parish
    async with db_session() as session:
        member_teams = await volunteer_team_ids(session, ids["member_vid"])
        outsider_teams = await volunteer_team_ids(session, ids["outsider_vid"])

    for name in ("leader", "second"):
        actor = await _actor(accounts, name)
        assert actor.can_view_capacity(member_teams), f"{name} sees their people's capacity"
        assert not actor.can_view_capacity(outsider_teams), "not other ministries' people"

    core = await _actor(accounts, "core")
    assert not core.can_view_capacity(member_teams), "core members never see capacity"

    member = await _actor(accounts, "member")
    assert not member.can_view_capacity(member_teams), "not even one's own capacity"

    admin = await _actor(accounts, "admin")
    assert admin.can_view_capacity(member_teams)
    assert admin.can_view_capacity(outsider_teams)
