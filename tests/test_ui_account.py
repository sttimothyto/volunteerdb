"""Self-service password management on /account.

The interesting rule is who has to re-type the old password: a session that
signed in *with* it does, a session that signed in with an emailed code does
not — that second case is what makes "I forgot my password" self-serviceable
without an admin (NIST SP 800-63B §4.1.2.1 counts it as binding a new
authenticator, not account recovery).
"""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.services import mail, users

from .conftest import SLOW, mail_to

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_otp_session_sets_a_password_without_the_old_one(database, monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> bool:
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(mail, "send_email", fake_send)

    async with db_session() as session:
        user = await users.create(
            session, "forgetful@example.org", password="cedar lamp figs"
        )
        user_id = user.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{user_id}?method=otp")
        await user.should_see("dev-login ok")
        await user.open("/account")
        await user.should_see("Change your password")
        await user.should_see("without the old one")

        # the policy speaks in the form, with the reason and the guidance
        user.find(marker="new-password").type("hunter2")
        user.find("Save password", kind=ui.button).click()
        await user.should_see("That password is too short", retries=SLOW)

        user.find(marker="new-password").clear()
        user.find(marker="new-password").type("thistle brook lantern")
        user.find(marker="repeat-password").type("thistle brook lantern")
        user.find("Save password", kind=ui.button).click()
        await user.should_see("Password saved", retries=SLOW)

    async with db_session() as session:
        assert (
            await users.authenticate(
                session, "forgetful@example.org", "thistle brook lantern"
            )
            is not None
        )
        assert (
            await users.authenticate(
                session, "forgetful@example.org", "cedar lamp figs"
            )
            is None
        ), "the old password is gone"

    # §4.1.2: the account's address hears about it, through a channel the
    # browser doing the change does not control
    _to, subject, _body = await mail_to(sent, "forgetful@example.org")
    assert subject == "Your VolunteerDB password changed"


async def test_password_session_must_retype_the_current_password(database, monkeypatch):
    monkeypatch.setattr(mail, "send_email", lambda *a, **k: _ok())

    async with db_session() as session:
        user = await users.create(
            session, "careful@example.org", password="cedar lamp figs"
        )
        user_id = user.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{user_id}")  # signed in with the password
        await user.open("/account")
        await user.should_see("Current password")

        user.find(marker="new-password").type("thistle brook lantern")
        user.find(marker="repeat-password").type("thistle brook lantern")
        user.find(marker="current-password").type("not-the-old-one-at-all")
        user.find("Save password", kind=ui.button).click()
        await user.should_see("not your current password", retries=SLOW)

        user.find(marker="current-password").clear()
        user.find(marker="current-password").type("cedar lamp figs")
        user.find("Save password", kind=ui.button).click()
        await user.should_see("Password saved", retries=SLOW)

    async with db_session() as session:
        assert (
            await users.authenticate(
                session, "careful@example.org", "thistle brook lantern"
            )
            is not None
        )


async def _ok() -> bool:
    return True
