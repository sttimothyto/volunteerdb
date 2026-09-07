"""Headless UI test: the dashboard's statistic tiers, per role.

Ordering down the page is the feature — parish, then leadership, then the
reader's own teams and service, and the graph last of all — but what a test
can mostly hold onto is which sections exist at all for whom. A section that
is absent here was never computed by services.stats; see tests/test_stats.py
for that half.
"""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok

# distinctive tile captions, not the section headings: "Parish" also appears
# in the graph filter's "— whole parish —" option
PARISH_TILE = "Active volunteers"
LEADERSHIP_TILE = "Teams I help run"
PERSONAL_TILE = "Hours served"


async def _parish(session):
    liturgy = ok(await teams.create(session, SYSTEM, "Liturgy"))
    music = ok(await teams.create(session, SYSTEM, "Music", parent_team_id=liturgy.id))

    lea = ok(await volunteers.create(session, SYSTEM, "Lea", "Der", "lea@example.org"))
    cora = ok(
        await volunteers.create(session, SYSTEM, "Cora", "Core", "cora@example.org")
    )
    mel = ok(
        await volunteers.create(session, SYSTEM, "Mel", "Ember", "mel@example.org")
    )
    ok(await memberships.assign(session, SYSTEM, lea.id, liturgy.id, TeamRole.leader))
    ok(await memberships.assign(session, SYSTEM, cora.id, liturgy.id, TeamRole.core))
    ok(await memberships.assign(session, SYSTEM, mel.id, music.id, TeamRole.member))

    admin, _ = ok(
        await users.create(
            session,
            "admin@example.org",
            is_admin=True,
            password="test-pass-phrase",
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    accounts = {"admin": admin.id}
    for name, volunteer in (("lea", lea), ("cora", cora), ("mel", mel)):
        user, _ = ok(
            await users.create(
                session,
                f"{name}@example.org",
                volunteer_id=volunteer.id,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
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
        # the guides are three for the highest role -- an admin's -- and the
        # way into the rest
        await user.should_see("Administer the parish")
        await user.should_see("Manage accounts")
        await user.should_see("All guides")
        await user.should_not_see("Lead a team")


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
        await user.should_see("Lead a team")  # the leaders' tutorial
        await user.should_not_see("Administer the parish")


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
        await user.should_see("Read the full roster of your team")
        await user.should_not_see("Lead a team")


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

        # The reader first: My service, then My teams, then the guides, then
        # the folded graph. NiceGUI hands out element ids in creation order,
        # which is render order down the page — the only handle a headless
        # run has on "above".
        teams_head = only(user.find("My teams", kind=ui.label))
        service_head = only(user.find("My service", kind=ui.label))
        guides_head = only(user.find("Guides", kind=ui.label))
        graph = only(user.find(marker="graph-panel"))
        assert service_head.id < teams_head.id < guides_head.id < graph.id

        # a plain member gets the members' three, and none of a core member's
        await user.should_see("Your teams and your service")
        await user.should_see("Update your contact details")
        await user.should_not_see("Read the full roster of your team")
