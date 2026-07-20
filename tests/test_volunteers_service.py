"""Volunteer service: update sentinel semantics, search, normalization, impact."""

import pytest

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, volunteers


async def test_create_normalizes_fields(database):
    async with db_session() as session:
        v = await volunteers.create(session, "  Maria ", " Alvarez  ", "  Maria@Example.ORG ")
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

        cleared = await volunteers.update(session, v.id, email=None, phone=None, notes=None)
        assert cleared.email is None and cleared.phone is None and cleared.notes is None

        recased = await volunteers.update(session, v.id, email="  NEW@Example.ORG ")
        assert recased.email == "new@example.org"


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
        assert [r.team.name for r in rows] == ["Solo-led", "Well-backed"], "most critical first"
        assert rows[0].leaders_left == 0 and rows[0].leadership_left == 0
        assert rows[1].leaders_left == 1 and rows[1].leadership_left == 2
        assert rows[0].role == TeamRole.leader
