"""Headless UI test: the dashboard's statistic tiers, per role.

Ordering down the page is the feature — parish, then leadership, then the
reader's own teams and service, and the graph last of all — but what a test
can mostly hold onto is which sections exist at all for whom. A section that
is absent here was never computed by services.stats; see tests/test_stats.py
for that half.
"""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.ui.cytoscape_element import CytoscapeGraph

from tests.fp_helpers import ok

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

# distinctive tile captions, not the section headings: "Parish" also appears
# in the graph filter's "— whole parish —" option
PARISH_TILE = "Active volunteers"
LEADERSHIP_TILE = "Teams I help run"
PERSONAL_TILE = "Hours served"
SLOW = 30


async def _parish(session):
    liturgy = ok(await teams.create(session, None, "Liturgy"))
    music = ok(await teams.create(session, None, "Music", parent_team_id=liturgy.id))

    lea = ok(await volunteers.create(session, None, "Lea", "Der", "lea@example.org"))
    cora = ok(
        await volunteers.create(session, None, "Cora", "Core", "cora@example.org")
    )
    mel = ok(await volunteers.create(session, None, "Mel", "Ember", "mel@example.org"))
    ok(await memberships.assign(session, None, lea.id, liturgy.id, TeamRole.leader))
    ok(await memberships.assign(session, None, cora.id, liturgy.id, TeamRole.core))
    ok(await memberships.assign(session, None, mel.id, music.id, TeamRole.member))

    admin, _ = await users.create(
        session, "admin@example.org", is_admin=True, password="test-pass-phrase"
    )
    accounts = {"admin": admin.id}
    for name, volunteer in (("lea", lea), ("cora", cora), ("mel", mel)):
        user, _ = await users.create(
            session,
            f"{name}@example.org",
            volunteer_id=volunteer.id,
            password="test-pass-phrase",
        )
        accounts[name] = user.id
    return accounts


async def test_admin_sees_every_tier(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin']}")
        await user.should_see("dev-login ok")
        await user.open("/")

        await user.should_see(PARISH_TILE, retries=SLOW)
        await user.should_see("Without a leader")
        await user.should_see("No email address")
        # an admin's scope is the whole parish, so the reach tiles would only
        # repeat the parish section above and are left out
        await user.should_not_see(LEADERSHIP_TILE)
        # no volunteer record behind this account, so no own-service section
        await user.should_not_see(PERSONAL_TILE)


async def test_leader_sees_leadership_but_not_the_parish(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lea']}")
        await user.should_see("dev-login ok")
        await user.open("/")

        await user.should_see(LEADERSHIP_TILE, retries=SLOW)
        await user.should_see("Without a leader", retries=SLOW)
        await user.should_see(PERSONAL_TILE)
        await user.should_not_see(PARISH_TILE)


async def test_core_member_sees_reach_without_coverage_or_workload(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['cora']}")
        await user.should_see("dev-login ok")
        await user.open("/")

        await user.should_see(LEADERSHIP_TILE, retries=SLOW)
        await user.should_not_see(PARISH_TILE)
        await user.should_not_see("Without a leader")
        await user.should_not_see("Workload:")


async def test_plain_member_sees_only_their_own_service(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mel']}")
        await user.should_see("dev-login ok")
        await user.open("/")

        await user.should_see(PERSONAL_TILE, retries=SLOW)
        await user.should_see("My teams")
        await user.should_not_see(PARISH_TILE)
        await user.should_not_see(LEADERSHIP_TILE)
        # workload is a leadership signal and never turns up on one's own page
        await user.should_not_see("Workload:")

        # My teams, then My service, then the graph. NiceGUI hands out element
        # ids in creation order, which is render order down the page — the only
        # handle a headless run has on "above".
        teams_head = user.find("My teams", kind=ui.label).elements.pop()
        service_head = user.find("My service", kind=ui.label).elements.pop()
        graph = user.find(kind=CytoscapeGraph).elements.pop()
        assert teams_head.id < service_head.id < graph.id
