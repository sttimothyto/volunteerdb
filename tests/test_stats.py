"""Dashboard statistics: the numbers, and who is allowed to be told them.

The access-control assertions here are the point of the module. A tier that
comes back None was never computed, so these tests are checking that the
service refuses to answer, not that the page hides an answer it holds.
"""

from datetime import UTC, datetime, timedelta

from volunteerdb.actors import load_actor
from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import (
    memberships,
    stats,
    teams,
    users,
    volunteers,
)

from tests import mint
from tests.fp_helpers import ok


async def _parish(session):
    """Liturgy (leader + second + core + member) with a Music sub-team, plus
    an unrelated Hospitality team that has nobody leading it."""
    liturgy = ok(await teams.create(session, None, "Liturgy"))
    music = ok(await teams.create(session, None, "Music", parent_team_id=liturgy.id))
    hospitality = ok(await teams.create(session, None, "Hospitality"))

    lea = ok(await volunteers.create(session, None, "Lea", "Der", "lea@example.org"))
    sam = ok(await volunteers.create(session, None, "Sam", "Second", "sam@example.org"))
    cora = ok(
        await volunteers.create(session, None, "Cora", "Core", "cora@example.org")
    )
    mel = ok(
        await volunteers.create(session, None, "Mel", "Ember")
    )  # no email on purpose
    solo = ok(
        await volunteers.create(session, None, "Solo", "Nobody", "solo@example.org")
    )

    ok(await memberships.assign(session, None, lea.id, liturgy.id, TeamRole.leader))
    ok(await memberships.assign(session, None, sam.id, liturgy.id, TeamRole.second))
    ok(await memberships.assign(session, None, cora.id, liturgy.id, TeamRole.core))
    ok(await memberships.assign(session, None, mel.id, music.id, TeamRole.member))
    # one person on two teams, so "assignments" and "people" differ
    ok(await memberships.assign(session, None, lea.id, hospitality.id, TeamRole.member))

    return {
        "liturgy": liturgy,
        "music": music,
        "hospitality": hospitality,
        "lea": lea,
        "sam": sam,
        "cora": cora,
        "mel": mel,
        "solo": solo,
    }


async def _actor(session, email, **kwargs):
    user, _ = ok(
        await users.create(
            session,
            email,
            password="test-pass-phrase",
            **kwargs,
            invite=mint.fresh_invite(),
        )
    )
    return await load_actor(session, user)


async def test_parish_tier_counts(database):
    async with db_session() as session:
        p = await _parish(session)
        ok(await volunteers.update(session, None, p["solo"].id, is_active=False))
        ok(
            await users.create(
                session,
                "lea@example.org",
                volunteer_id=p["lea"].id,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        actor = await _actor(session, "admin@example.org", is_admin=True)

        figures = await stats.dashboard(session, actor)

        parish = figures.parish
        assert parish is not None
        assert parish.active_volunteers == 4, "Solo was retired"
        assert parish.inactive_volunteers == 1
        assert parish.active_teams == 3
        assert parish.assignments == 5, "Lea's two memberships count twice"
        assert parish.ministries_per_volunteer == 1.2
        assert parish.unassigned_volunteers == 0, "the only teamless one is inactive"
        assert parish.accounts == 1, (
            "Lea's; the admin's own account is not linked to a volunteer, and "
            "the tile is about volunteers who can sign in"
        )
        assert figures.live is True


async def test_parish_counts_active_volunteers_on_no_team(database):
    async with db_session() as session:
        await _parish(session)  # Solo stays active and stays teamless
        actor = await _actor(session, "admin@example.org", is_admin=True)

        figures = await stats.dashboard(session, actor)

        assert figures.parish is not None
        assert figures.parish.active_volunteers == 5
        assert figures.parish.unassigned_volunteers == 1


async def test_admin_sees_gaps_workload_and_the_whole_parish(database):
    async with db_session() as session:
        p = await _parish(session)
        actor = await _actor(session, "admin@example.org", is_admin=True)

        lead = (await stats.dashboard(session, actor)).leadership

        assert lead is not None
        assert lead.teams == 3, "an admin's scope is every team"
        assert lead.people == 4, "distinct people holding a membership"
        assert lead.people_without_email == 1, "Mel"
        assert lead.teams_without_leader == 2, "Music and Hospitality"
        assert lead.teams_without_second == 2
        gap_ids = {g.team_id for g in lead.gap_teams}
        assert gap_ids == {p["music"].id, p["hospitality"].id}
        assert lead.bands is not None
        assert sum(b.count for b in lead.bands) == 4, "every volunteer is scored"


async def test_leader_sees_only_their_subtree(database):
    async with db_session() as session:
        p = await _parish(session)
        actor = await _actor(session, "lea@example.org", volunteer_id=p["lea"].id)

        lead = (await stats.dashboard(session, actor)).leadership

        assert lead is not None
        # leading Liturgy cascades to Music; Hospitality membership is plain,
        # so it never joins the managed scope
        assert lead.teams == 2, "Liturgy and Music, not Hospitality"
        assert lead.people == 4, "Lea, Sam, Cora on Liturgy; Mel on Music"
        assert lead.teams_without_leader == 1, "Music only — not Hospitality"
        assert {g.team_id for g in lead.gap_teams} == {p["music"].id}


async def test_core_member_sees_reach_but_no_gaps_and_no_workload(database):
    async with db_session() as session:
        p = await _parish(session)
        actor = await _actor(session, "cora@example.org", volunteer_id=p["cora"].id)

        figures = await stats.dashboard(session, actor)

        assert figures.parish is None, "the parish tier is admins only"
        lead = figures.leadership
        assert lead is not None, "core reads full rosters, so the section renders"
        assert lead.teams == 2, "core of Liturgy cascades to Music"
        assert lead.people == 4
        assert lead.people_without_email == 1
        assert lead.teams_without_leader is None, (
            "coverage is gated on managing the team, as on /teams and the API"
        )
        assert lead.gap_teams == ()
        assert lead.bands is None, "workload is never shown to core members"


async def test_plain_member_gets_no_leadership_tier_at_all(database):
    async with db_session() as session:
        p = await _parish(session)
        actor = await _actor(session, "mel@example.org", volunteer_id=p["mel"].id)

        figures = await stats.dashboard(session, actor)

        assert figures.parish is None
        assert figures.leadership is None
        assert figures.personal is not None, "their own service is still theirs"


async def test_workload_covers_only_people_the_actor_may_see(database):
    async with db_session() as session:
        p = await _parish(session)
        actor = await _actor(session, "lea@example.org", volunteer_id=p["lea"].id)

        lead = (await stats.dashboard(session, actor)).leadership

        assert lead is not None and lead.bands is not None
        # Lea leads Liturgy+Music, so she may see those four people's scores.
        # Nobody else exists in her managed scope — and can_view_workload
        # excludes nobody here only because she leads their teams.
        assert sum(b.count for b in lead.bands) == 4


async def test_admin_without_a_volunteer_has_no_personal_tier(database):
    async with db_session() as session:
        await _parish(session)
        actor = await _actor(session, "admin@example.org", is_admin=True)

        figures = await stats.dashboard(session, actor)

        assert figures.parish is not None
        assert figures.personal is None, "no volunteer record, no own service"


async def test_personal_tier_is_empty_until_there_is_anything_to_report(database):
    async with db_session() as session:
        p = await _parish(session)
        actor = await _actor(session, "mel@example.org", volunteer_id=p["mel"].id)

        mine = (await stats.dashboard(session, actor)).personal

        assert mine is not None
        assert mine.upcoming_duties == 0
        assert mine.next_duty_at is None
        assert mine.claimable_subs == 0
        assert mine.ballots_waiting == 0
        assert mine.events_attended == 0


async def test_as_of_drops_the_live_only_figures(database):
    async with db_session() as session:
        await _parish(session)
        actor = await _actor(session, "admin@example.org", is_admin=True)

        at = datetime.now(UTC) - timedelta(seconds=1)
        figures = await stats.dashboard(session, actor, at=at)

        assert figures.live is False
        assert figures.parish is not None
        assert figures.parish.accounts is None, "app_user is not versioned"
        assert figures.leadership is not None
        assert figures.leadership.understaffed_events is None
        assert figures.leadership.open_elections is None
        assert figures.leadership.teams_without_leader is not None, (
            "coverage is versioned, so a snapshot can still answer it"
        )
        assert figures.personal is None, "service now is not a snapshot question"


async def test_as_of_before_the_parish_existed_counts_nothing(database):
    async with db_session() as session:
        await _parish(session)
        actor = await _actor(session, "admin@example.org", is_admin=True)

        at = datetime.now(UTC) - timedelta(days=365)
        figures = await stats.dashboard(session, actor, at=at)

        assert figures.parish is not None
        assert figures.parish.active_volunteers == 0
        assert figures.parish.assignments == 0
