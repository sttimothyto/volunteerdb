"""Render smoke tests for the admin pages (previously zero UI coverage)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_admin_pages_render(database):
    async with db_session() as session:
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        member = await users.create(
            session, "felix@example.org", password="test-pass-phrase"
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

        await user.open("/import")
        await user.should_see("Full parish export")

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
        await user.open("/import")
        await user.should_see(
            "Import/Export is available to admins and to team leaders/seconds."
        )
        await user.open("/elections")
        await user.should_see("Elections are available to admins")


async def test_leader_import_page_scoped(database):
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
        await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
        leader = await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            password="test-pass-phrase",
        )
        leader_id = leader.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{leader_id}")
        await user.should_see("dev-login ok")
        await user.open("/import")
        await user.should_see("My teams export")
        await user.should_see("Import/Export")  # nav link visible to leaders


async def test_admin_users_provision_button(database, monkeypatch):
    from volunteerdb.services import mail

    async def fake_send(to: str, subject: str, body: str) -> bool:
        return True

    monkeypatch.setattr(mail, "send_email", fake_send)

    async with db_session() as session:
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        await volunteers.create(session, "Vera", "Volunteer", "vera@example.org")
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
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        orphan = await users.create(
            session, "orphan@example.org", password="test-pass-phrase"
        )
        vera = await volunteers.create(session, "Vera", "Volunteer", "vera@example.org")
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
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        pending = await users.create(session, "pending@example.org")
        stale = await users.create(session, "stale@example.org")
        stale.invite_expires_at = datetime.now(UTC) - timedelta(hours=1)
        await session.flush()
        admin_id = admin.id
        assert users.invite_live(pending) and not users.invite_live(stale)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/admin/users")
        await user.should_see("invite pending")
        await user.should_see("invite expired")
