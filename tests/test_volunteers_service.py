"""Volunteer service: update sentinel semantics, search, normalization, impact."""

from volunteerdb import errors
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, volunteers

from tests import mint
from tests.conftest import db_session
from tests.fp_helpers import done, ok, refused


async def test_create_normalizes_fields(database):
    async with db_session() as session:
        v = ok(
            await volunteers.create(
                session, None, "  Maria ", " Alvarez  ", "  Maria@Example.ORG "
            )
        )
        assert (v.first_name, v.last_name) == ("Maria", "Alvarez")
        assert v.email == "maria@example.org"

        no_email = ok(await volunteers.create(session, None, "Nadia", "Noemail"))
        assert no_email.email is None


async def test_update_unset_vs_none_semantics(database):
    async with db_session() as session:
        v = ok(
            await volunteers.create(
                session, None, "Ann", "Baker", "ann@example.org", "555-1", "some notes"
            )
        )

        renamed = done(
            await volunteers.update(session, None, v.id, first_name="Anne")
        ).value
        assert renamed.email == "ann@example.org", "omitted fields stay untouched"
        assert renamed.phone == "555-1" and renamed.notes == "some notes"

        cleared = done(
            await volunteers.update(
                session, None, v.id, email=None, phone=None, notes=None
            )
        ).value
        assert cleared.email is None and cleared.phone is None and cleared.notes is None

        recased = done(
            await volunteers.update(session, None, v.id, email="  NEW@Example.ORG ")
        ).value
        assert recased.email == "new@example.org"


async def test_name_map_orders_and_filters(database):
    """The dropdown helper: matches search()'s ordering and active-only
    default without hydrating full entities."""
    async with db_session() as session:
        z = ok(await volunteers.create(session, None, "Ann", "Zed"))
        a = ok(await volunteers.create(session, None, "Bea", "Able"))
        gone = ok(await volunteers.create(session, None, "Faded", "Ghost"))
        done(await volunteers.update(session, None, gone.id, is_active=False))

        active = await volunteers.name_map(session)
        assert list(active.items()) == [(a.id, "Bea Able"), (z.id, "Ann Zed")], (
            "ordered by last, first; inactive volunteers absent by default"
        )
        assert gone.id in await volunteers.name_map(session, include_inactive=True)


async def test_search_by_name_email_and_inactive_flag(database):
    async with db_session() as session:
        a = ok(
            await volunteers.create(session, None, "Maria", "Alvarez", "maria@one.org")
        )
        ok(await volunteers.create(session, None, "Bruno", "Costa", "bruno@two.org"))
        gone = ok(await volunteers.create(session, None, "Faded", "Ghost"))
        done(await volunteers.update(session, None, gone.id, is_active=False))

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
            ok(
                await volunteers.create(
                    session, None, "Sam", last, f"sam.{last}@example.org"
                )
            )

        assert len(await volunteers.search(session, "Sam")) == 4, "unlimited by default"

        capped = await volunteers.search(session, "Sam", limit=2)
        assert [v.last_name for v in capped] == ["Alpha", "Bravo"]


async def test_search_private_fields_are_scope_aware(database):
    from volunteerdb.actors import load_actor
    from volunteerdb.services import custom_fields, users

    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        garden = ok(await teams.create(session, None, "Garden"))
        insider = ok(
            await volunteers.create(session, None, "Inne", "Sider", None, "555-0100")
        )
        outsider = ok(
            await volunteers.create(
                session,
                None,
                "Outt",
                "Sider",
                None,
                "555-0200",
                "keeps the secret recipe",
            )
        )
        plain = ok(
            await volunteers.create(session, None, "Plain", "Member", None, "555-0300")
        )
        ok(
            await memberships.assign(
                session, None, insider.id, liturgy.id, TeamRole.member
            )
        )
        ok(
            await memberships.assign(
                session, None, outsider.id, garden.id, TeamRole.member
            )
        )
        ok(
            await memberships.assign(
                session, None, plain.id, liturgy.id, TeamRole.member
            )
        )
        ok(await custom_fields.create_def(session, None, "Training", "text"))
        ok(
            await custom_fields.set_values(
                session, None, insider.id, {"training": "lector-certified"}
            )
        )

        leader_v = ok(await volunteers.create(session, None, "Lena", "Leader"))
        ok(
            await memberships.assign(
                session, None, leader_v.id, liturgy.id, TeamRole.leader
            )
        )
        leader = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "lena@example.org",
                        volunteer_id=leader_v.id,
                        invite=mint.fresh_invite(),
                    )
                )
            )[0],
        )
        admin = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "admin@example.org",
                        is_admin=True,
                        invite=mint.fresh_invite(),
                    )
                )
            )[0],
        )
        member = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "plain@example.org",
                        volunteer_id=plain.id,
                        invite=mint.fresh_invite(),
                    )
                )
            )[0],
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

        # email is scoped with the rest of the contact details, not public.
        # The list renders it as `•••` to these viewers, and a bare match let
        # the query language walk it out a character at a time.
        done(
            await volunteers.update(
                session, None, outsider.id, email="outt@example.org"
            )
        )
        assert await volunteers.search(session, "outt@example", actor=member) == [], (
            "an address a member may not read must not confirm by row presence"
        )
        assert await volunteers.search(session, "outt@example", actor=leader) == [], (
            "nor for a leader of another ministry"
        )
        assert [
            v.id for v in await volunteers.search(session, "outt@example", actor=admin)
        ] == [outsider.id], "admins still match every column"
        done(
            await volunteers.update(
                session, None, plain.id, email="plain.v@example.org"
            )
        )
        assert [
            v.id
            for v in await volunteers.search(session, "plain.v@example", actor=member)
        ] == [plain.id], "your own address is still yours to search"


async def test_missing_volunteer_raises_lookup(database):
    async with db_session() as session:
        assert await volunteers.get(session, 424242) is None
        refused(
            await volunteers.update(session, None, 424242, first_name="X"),
            errors.NotFound,
        )
        refused(await volunteers.delete(session, None, 424242), errors.NotFound)


async def test_impact_counts_and_critical_first_ordering(database):
    async with db_session() as session:
        solo = ok(await teams.create(session, None, "Solo-led"))
        backed = ok(await teams.create(session, None, "Well-backed"))
        v = ok(await volunteers.create(session, None, "Key", "Person"))
        other_lead = ok(await volunteers.create(session, None, "Other", "Leader"))
        other_second = ok(await volunteers.create(session, None, "Other", "Second"))

        ok(await memberships.assign(session, None, v.id, solo.id, TeamRole.leader))
        ok(await memberships.assign(session, None, v.id, backed.id, TeamRole.member))
        ok(
            await memberships.assign(
                session, None, other_lead.id, backed.id, TeamRole.leader
            )
        )
        ok(
            await memberships.assign(
                session, None, other_second.id, backed.id, TeamRole.second
            )
        )

        rows = ok(await volunteers.impact(session, None, v.id))
        assert [r.team.name for r in rows] == ["Solo-led", "Well-backed"], (
            "most critical first"
        )
        assert rows[0].leaders_left == 0 and rows[0].leadership_left == 0
        assert rows[1].leaders_left == 1 and rows[1].leadership_left == 2
        assert rows[0].role == TeamRole.leader


async def test_search_or_query_scopes_rows_per_role(database):
    """The WHERE-filter twin of test_search_private_fields_are_scope_aware:
    a private-field predicate is false outside the actor's visibility, in
    both polarities, and membership predicates see only visible rosters."""
    from volunteerdb.actors import load_actor
    from volunteerdb.services import custom_fields, users

    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        garden = ok(await teams.create(session, None, "Garden"))
        insider = ok(
            await volunteers.create(session, None, "Inne", "Sider", None, "555-0100")
        )
        gardener = ok(
            await volunteers.create(
                session,
                None,
                "Gard",
                "Ener",
                None,
                "555-0200",
                "keeps the secret recipe",
            )
        )
        plain = ok(
            await volunteers.create(session, None, "Plain", "Member", None, "555-0300")
        )
        ok(
            await memberships.assign(
                session, None, insider.id, liturgy.id, TeamRole.member
            )
        )
        ok(
            await memberships.assign(
                session, None, gardener.id, garden.id, TeamRole.leader
            )
        )
        ok(
            await memberships.assign(
                session, None, plain.id, liturgy.id, TeamRole.member
            )
        )
        ok(await custom_fields.create_def(session, None, "Years served", "integer"))
        ok(
            await custom_fields.set_values(
                session, None, insider.id, {"years_served": 10}
            )
        )
        ok(await custom_fields.set_values(session, None, plain.id, {"years_served": 9}))

        leader_v = ok(await volunteers.create(session, None, "Lena", "Leader"))
        ok(
            await memberships.assign(
                session, None, leader_v.id, liturgy.id, TeamRole.leader
            )
        )
        leader = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "lena@example.org",
                        volunteer_id=leader_v.id,
                        invite=mint.fresh_invite(),
                    )
                )
            )[0],
        )
        admin = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "admin@example.org",
                        is_admin=True,
                        invite=mint.fresh_invite(),
                    )
                )
            )[0],
        )
        member = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "plain@example.org",
                        volunteer_id=plain.id,
                        invite=mint.fresh_invite(),
                    )
                )
            )[0],
        )
        gard = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "gard@example.org",
                        volunteer_id=gardener.id,
                        invite=mint.fresh_invite(),
                    )
                )
            )[0],
        )

        async def ids(text, actor):
            return {
                v.id
                for v in ok(
                    await volunteers.search_or_query(session, text, actor=actor)
                )
            }

        # public fields answer identically for every role
        for actor in (admin, leader, member, None):
            assert await ids("first_name = 'Inne'", actor) == {insider.id}

        # private leaves: false outside the actor's visibility...
        assert await ids("phone LIKE '555%'", admin) == {
            insider.id,
            gardener.id,
            plain.id,
        }
        assert await ids("phone LIKE '555%'", leader) == {insider.id, plain.id}
        assert await ids("phone LIKE '555%'", member) == {plain.id}, (
            "member role sees own row only"
        )
        # ...in the negated polarity too: everyone's phone differs from
        # 555-0100 except the insider's, but a member may learn that about
        # nobody but themself
        assert await ids("NOT (phone LIKE '555-0100')", member) == {plain.id}
        assert await ids("notes IS NULL", member) == {plain.id}, (
            "IS NULL is a private predicate like any other"
        )

        # roster leaves see only rosters the actor may view
        assert await ids("team = 'Liturgy'", admin) == {
            insider.id,
            plain.id,
            leader_v.id,
        }
        assert await ids("team = 'Liturgy'", member) == {
            insider.id,
            plain.id,
            leader_v.id,
        }, "a direct member may match their own team's roster"
        assert await ids("team = 'Liturgy'", gard) == set(), (
            "an outsider's membership predicate finds nothing"
        )
        assert await ids("role = 'leader'", admin) == {gardener.id, leader_v.id}
        assert await ids("role IN ('leader', 'second')", member) == {leader_v.id}

        # negated roster: "holds no such visible membership"
        assert await ids("team != 'Liturgy'", admin) == {gardener.id}
        assert await ids("team != 'Liturgy'", gard) == {
            insider.id,
            gardener.id,
            plain.id,
            leader_v.id,
        }, "invisible memberships cannot prove membership either way"
        assert await ids("team IS NULL", admin) == set()

        # typed custom-field comparisons (private tier): numeric, not lexical
        assert await ids("custom.years_served > 9", admin) == {insider.id}
        assert await ids("years_served BETWEEN 9 AND 10", admin) == {
            insider.id,
            plain.id,
        }
        assert await ids("years_served IS NULL", admin) == {gardener.id, leader_v.id}
        assert await ids("years_served > 2", member) == {plain.id}, (
            "custom values scope like contact details"
        )

        # non-queries fall back to plain substring search
        assert await ids("Inne", admin) == {insider.id}
        refused(
            await volunteers.search_or_query(session, "bogus = 1", actor=admin),
            errors.QueryError,
        )


async def test_search_or_query_inactive_and_as_of(database):
    from datetime import UTC, datetime

    async with db_session() as session:
        v = ok(
            await volunteers.create(session, None, "Before", "Rename", "b@example.org")
        )
        gone = ok(await volunteers.create(session, None, "Faded", "Ghost"))
        done(await volunteers.update(session, None, gone.id, is_active=False))
        vid, gone_id = v.id, gone.id

    when = datetime.now(UTC)
    async with db_session() as session:
        done(await volunteers.update(session, None, vid, first_name="After"))

    async with db_session() as session:
        found = ok(await volunteers.search_or_query(session, "first_name = 'After'"))
        assert [x.id for x in found] == [vid]
        assert (
            ok(await volunteers.search_or_query(session, "first_name = 'Before'")) == []
        )

        past = ok(
            await volunteers.search_or_query(session, "first_name = 'Before'", at=when)
        )
        assert [x.id for x in past] == [vid], "queries respect as-of"

        assert (
            ok(await volunteers.search_or_query(session, "last_name = 'Ghost'")) == []
        )
        withall = ok(
            await volunteers.search_or_query(
                session, "last_name = 'Ghost'", include_inactive=True
            )
        )
        assert [x.id for x in withall] == [gone_id]
        assert (
            ok(await volunteers.search_or_query(session, "is_active = false")) == []
        ), "is_active cannot widen what include_inactive gates"
