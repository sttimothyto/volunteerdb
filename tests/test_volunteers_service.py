"""Volunteer service: update sentinel semantics, search, normalization, impact."""

import pytest

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, volunteers


async def test_create_normalizes_fields(database):
    async with db_session() as session:
        v = await volunteers.create(
            session, "  Maria ", " Alvarez  ", "  Maria@Example.ORG "
        )
        assert (v.first_name, v.last_name) == ("Maria", "Alvarez")
        assert v.email == "maria@example.org"

        no_email = await volunteers.create(session, "Nadia", "Noemail")
        assert no_email.email is None


async def test_update_unset_vs_none_semantics(database):
    async with db_session() as session:
        v = await volunteers.create(
            session, "Ann", "Baker", "ann@example.org", "555-1", "some notes"
        )

        renamed = await volunteers.update(session, v.id, first_name="Anne")
        assert renamed.email == "ann@example.org", "omitted fields stay untouched"
        assert renamed.phone == "555-1" and renamed.notes == "some notes"

        cleared = await volunteers.update(
            session, v.id, email=None, phone=None, notes=None
        )
        assert cleared.email is None and cleared.phone is None and cleared.notes is None

        recased = await volunteers.update(session, v.id, email="  NEW@Example.ORG ")
        assert recased.email == "new@example.org"


async def test_name_map_orders_and_filters(database):
    """The dropdown helper: matches search()'s ordering and active-only
    default without hydrating full entities."""
    async with db_session() as session:
        z = await volunteers.create(session, "Ann", "Zed")
        a = await volunteers.create(session, "Bea", "Able")
        gone = await volunteers.create(session, "Faded", "Ghost")
        await volunteers.update(session, gone.id, is_active=False)

        active = await volunteers.name_map(session)
        assert list(active.items()) == [(a.id, "Bea Able"), (z.id, "Ann Zed")], (
            "ordered by last, first; inactive volunteers absent by default"
        )
        assert gone.id in await volunteers.name_map(session, include_inactive=True)


async def test_search_by_name_email_and_inactive_flag(database):
    async with db_session() as session:
        a = await volunteers.create(session, "Maria", "Alvarez", "maria@one.org")
        await volunteers.create(session, "Bruno", "Costa", "bruno@two.org")
        gone = await volunteers.create(session, "Faded", "Ghost")
        await volunteers.update(session, gone.id, is_active=False)

        hits = await volunteers.search(session, "ria Alv")
        assert [v.id for v in hits] == [a.id], "matches across first+last name"

        hits = await volunteers.search(session, "two.org")
        assert [v.email for v in hits] == ["bruno@two.org"]

        assert "Ghost" not in {v.last_name for v in await volunteers.search(session)}
        withall = await volunteers.search(session, include_inactive=True)
        assert "Ghost" in {v.last_name for v in withall}


async def test_search_limit_caps_rows_in_name_order(database):
    async with db_session() as session:
        for last in ("Delta", "Alpha", "Charlie", "Bravo"):
            await volunteers.create(session, "Sam", last, f"sam.{last}@example.org")

        assert len(await volunteers.search(session, "Sam")) == 4, "unlimited by default"

        capped = await volunteers.search(session, "Sam", limit=2)
        assert [v.last_name for v in capped] == ["Alpha", "Bravo"]


async def test_search_private_fields_are_scope_aware(database):
    from volunteerdb.permissions import load_actor
    from volunteerdb.services import custom_fields, users

    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        garden = await teams.create(session, "Garden")
        insider = await volunteers.create(session, "Inne", "Sider", None, "555-0100")
        outsider = await volunteers.create(
            session, "Outt", "Sider", None, "555-0200", "keeps the secret recipe"
        )
        plain = await volunteers.create(session, "Plain", "Member", None, "555-0300")
        await memberships.assign(session, insider.id, liturgy.id, TeamRole.member)
        await memberships.assign(session, outsider.id, garden.id, TeamRole.member)
        await memberships.assign(session, plain.id, liturgy.id, TeamRole.member)
        await custom_fields.create_def(session, "Training", "text")
        await custom_fields.set_values(
            session, insider.id, {"training": "lector-certified"}
        )

        leader_v = await volunteers.create(session, "Lena", "Leader")
        await memberships.assign(session, leader_v.id, liturgy.id, TeamRole.leader)
        leader = await load_actor(
            session,
            await users.create(session, "lena@example.org", volunteer_id=leader_v.id),
        )
        admin = await load_actor(
            session, await users.create(session, "admin@example.org", is_admin=True)
        )
        member = await load_actor(
            session,
            await users.create(session, "plain@example.org", volunteer_id=plain.id),
        )

        # admins (and trusted internal callers, actor=None) match every column
        assert [
            v.id for v in await volunteers.search(session, "555-0200", actor=admin)
        ] == [outsider.id]
        assert [v.id for v in await volunteers.search(session, "secret recipe")] == [
            outsider.id
        ]
        assert [
            v.id
            for v in await volunteers.search(session, "lector-certified", actor=admin)
        ] == [insider.id], "custom-field values are searchable"

        # a leader matches private fields only inside their full-view subtree
        assert [
            v.id for v in await volunteers.search(session, "555-0100", actor=leader)
        ] == [insider.id]
        assert await volunteers.search(session, "555-0200", actor=leader) == [], (
            "another ministry's phone number must not confirm by row presence"
        )
        assert {
            v.id for v in await volunteers.search(session, "Sider", actor=leader)
        } == {
            insider.id,
            outsider.id,
        }, "names stay searchable for everyone"

        # a plain member matches private fields on themself only
        assert [
            v.id for v in await volunteers.search(session, "555-0300", actor=member)
        ] == [plain.id]
        assert await volunteers.search(session, "555-0100", actor=member) == [], (
            "member role grants names, not private-field search, even on their own team"
        )


async def test_missing_volunteer_raises_lookup(database):
    async with db_session() as session:
        assert await volunteers.get(session, 424242) is None
        with pytest.raises(LookupError):
            await volunteers.update(session, 424242, first_name="X")
        with pytest.raises(LookupError):
            await volunteers.delete(session, 424242)


async def test_impact_counts_and_critical_first_ordering(database):
    async with db_session() as session:
        solo = await teams.create(session, "Solo-led")
        backed = await teams.create(session, "Well-backed")
        v = await volunteers.create(session, "Key", "Person")
        other_lead = await volunteers.create(session, "Other", "Leader")
        other_second = await volunteers.create(session, "Other", "Second")

        await memberships.assign(session, v.id, solo.id, TeamRole.leader)
        await memberships.assign(session, v.id, backed.id, TeamRole.member)
        await memberships.assign(session, other_lead.id, backed.id, TeamRole.leader)
        await memberships.assign(session, other_second.id, backed.id, TeamRole.second)

        rows = await volunteers.impact(session, v.id)
        assert [r.team.name for r in rows] == ["Solo-led", "Well-backed"], (
            "most critical first"
        )
        assert rows[0].leaders_left == 0 and rows[0].leadership_left == 0
        assert rows[1].leaders_left == 1 and rows[1].leadership_left == 2
        assert rows[0].role == TeamRole.leader
