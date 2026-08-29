"""Render smoke tests for the admin pages (previously zero UI coverage)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import db_session
from tests.fp_helpers import ok

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_admin_pages_render(database):
    async with db_session() as session:
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        member, _ = ok(
            await users.create(
                session,
                "felix@example.org",
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        admin_id, member_id = admin.id, member.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        await user.open("/admin/users")
        await user.should_see("New account")
        await user.should_see("2 accounts")

        await user.open("/admin/fields")
        await user.should_see("New field")

        await user.open("/admin/workload")
        await user.should_see("Role multipliers")

        await user.open("/")
        await user.should_see("Focus on team")  # the merged ministry graph

        await user.open("/teams")
        user.find(kind=ui.table)  # the coverage table moved here

        await user.open("/elections")
        await user.should_see("Vacancies")

        # a non-admin gets the polite refusals, not the admin controls
        await user.open(f"/login-dev/{member_id}")
        await user.open("/admin/users")
        await user.should_see("Admins only.")
        await user.open("/elections")
        await user.should_see("Elections are available to admins")


async def test_leader_sees_scoped_export_on_teams_page(database):
    """The retired /import page's "My teams export" now lives on /teams, as a
    button whose scope follows the actor."""
    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        lena = ok(
            await volunteers.create(session, None, "Lena", "Leader", "lena@example.org")
        )
        ok(
            await memberships.assign(
                session, None, lena.id, liturgy.id, TeamRole.leader
            )
        )
        leader, _ = ok(
            await users.create(
                session,
                "lena@example.org",
                volunteer_id=lena.id,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        leader_id = leader.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{leader_id}")
        await user.should_see("dev-login ok")
        await user.open("/teams")
        await user.should_see("Export team(s)")
        # the nav link the page used to have is gone with the page
        await user.should_not_see("Import/Export")


async def test_admin_users_provision_button(database, monkeypatch):
    async with db_session() as session:
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        ok(
            await volunteers.create(
                session, None, "Vera", "Volunteer", "vera@example.org"
            )
        )
        admin_id = admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/admin/users")
        await user.should_see("1 accounts")

        user.find(
            "Create accounts for all volunteers with email", kind=ui.button
        ).click()
        await user.should_see("send each of them an invite email?", retries=30)
        user.find("Create and email invites", kind=ui.button).click()
        await user.should_see("vera@example.org", retries=30)
        await user.should_see("2 accounts", retries=30)


async def test_admin_users_relink_dialog(database):
    async with db_session() as session:
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        orphan, _ = ok(
            await users.create(
                session,
                "orphan@example.org",
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        vera = ok(
            await volunteers.create(
                session, None, "Vera", "Volunteer", "vera@example.org"
            )
        )
        admin_id, orphan_id, vera_id = admin.id, orphan.id, vera.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/admin/users")
        await user.should_see("not linked to a volunteer")

        user.find(marker=f"relink-{orphan_id}").click()
        await user.should_see("Linked volunteer for orphan@example.org", retries=30)
        user.find(marker=f"relink-pick-{orphan_id}").elements.pop().set_value(vera_id)
        user.find("Save", kind=ui.button).click()
        await user.should_see("orphan@example.org → Vera Volunteer", retries=30)

    async with db_session() as session:
        assert (await users.get(session, orphan_id)).volunteer_id == vera_id


async def test_admin_users_shows_invite_state(database):
    """The account row distinguishes a live invite link from a spent window —
    an admin should not hand out a link that has already stopped working."""
    async with db_session() as session:
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        pending, _ = ok(
            await users.create(
                session, "pending@example.org", invite=mint.fresh_invite()
            )
        )
        stale, _ = ok(
            await users.create(session, "stale@example.org", invite=mint.fresh_invite())
        )
        stale.invite_expires_at = datetime.now(UTC) - timedelta(hours=1)
        await session.flush()
        admin_id = admin.id
        assert users.invite_live(pending, now=mint.now()) and not users.invite_live(
            stale, now=mint.now()
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/admin/users")
        await user.should_see("invite pending")
        await user.should_see("invite expired")
