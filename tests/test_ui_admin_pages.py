"""Render smoke tests for the admin pages (previously zero UI coverage)."""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.services import users, volunteers

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_admin_pages_render(database):
    async with db_session() as session:
        admin = await users.create(session, "admin@example.org", is_admin=True, password="pw")
        member = await users.create(session, "felix@example.org", password="pw")
        admin_id, member_id = admin.id, member.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        await user.open("/admin/users")
        await user.should_see("New account")
        await user.should_see("2 accounts")

        await user.open("/admin/fields")
        await user.should_see("New field")

        await user.open("/admin/capacity")
        await user.should_see("Role multipliers")

        await user.open("/import")
        await user.should_see("Full parish export")

        await user.open("/")
        await user.should_see("Holes to fill")

        # a non-admin gets the polite refusals, not the admin controls
        await user.open(f"/login-dev/{member_id}")
        await user.open("/admin/users")
        await user.should_see("Admins only.")
        await user.open("/import")
        await user.should_see("Only admins can import or export the whole parish.")


async def test_admin_users_provision_button(database, monkeypatch):
    from volunteerdb.services import mail

    async def fake_send(to: str, subject: str, body: str) -> bool:
        return True

    monkeypatch.setattr(mail, "send_email", fake_send)

    async with db_session() as session:
        admin = await users.create(session, "admin@example.org", is_admin=True, password="pw")
        await volunteers.create(session, "Vera", "Volunteer", "vera@example.org")
        admin_id = admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/admin/users")
        await user.should_see("1 accounts")

        user.find("Create accounts for all volunteers with email", kind=ui.button).click()
        await user.should_see("send each of them an invite email?", retries=30)
        user.find("Create and email invites", kind=ui.button).click()
        await user.should_see("vera@example.org", retries=30)
        await user.should_see("2 accounts", retries=30)
