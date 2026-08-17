"""Headless UI tests: AuthMiddleware redirect, login guards, invite redemption."""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.services import mail, users

from .conftest import SLOW, mail_to

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_anonymous_redirect_and_login_guards(database):
    async with db_session() as session:
        await users.create(
            session, "admin@example.org", is_admin=True, password="correct-pass-phrase"
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        # an anonymous page hit bounces to the login card (AuthMiddleware)
        await user.open("/volunteers")
        await user.should_see("Volunteer Database (VDB)")

        # wrong password: notified, still on the login card
        user.find(kind=ui.input, content="Email").type("admin@example.org")
        user.find(kind=ui.input, content="Password (optional)").type("wrong-pw")
        user.find(kind=ui.input, content="Password (optional)").trigger("keydown.enter")
        await user.should_see("Invalid email or password", retries=SLOW)

        # correct password: redirect_to round-trips back to the page we wanted
        # (argon2 verify + navigation outlast should_see's default 0.3s window)
        user.find(kind=ui.input, content="Password (optional)").clear()
        user.find(kind=ui.input, content="Password (optional)").type(
            "correct-pass-phrase"
        )
        user.find(kind=ui.input, content="Password (optional)").trigger("keydown.enter")
        await user.should_see("0 volunteers", retries=SLOW)  # the /volunteers list

        # open-redirect guard: a scheme-relative target is ignored -> dashboard
        await user.open("/login?redirect_to=//evil.example")
        user.find(kind=ui.input, content="Email").type("admin@example.org")
        user.find(kind=ui.input, content="Password (optional)").type(
            "correct-pass-phrase"
        )
        user.find(kind=ui.input, content="Password (optional)").trigger("keydown.enter")
        await user.should_see("Find volunteers or teams…", retries=SLOW)


async def test_invite_redemption_flow(database, monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> bool:
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(mail, "send_email", fake_send)

    async with db_session() as session:
        invitee = await users.create(session, "new@example.org")
        token = invitee.invite_token

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/invite/{token}")
        await user.should_see("Finish your account setup")

        # the confidentiality notice must be agreed to before anything else
        user.find("Finish setup and sign in", kind=ui.button).click()
        await user.should_see("please agree to keep personal information", retries=SLOW)
        agree = next(
            cb for cb in user.find(kind=ui.checkbox).elements if "I agree" in cb.text
        )
        agree.value = True

        # rejected by the policy, with the reason (SP 800-63B: 15 characters)
        user.find(kind=ui.input, content="Password (optional)").type("short")
        user.find("Finish setup and sign in", kind=ui.button).click()
        await user.should_see("That password is too short", retries=SLOW)

        # ... and again for a long one that is on the blocklist
        user.find(kind=ui.input, content="Password (optional)").clear()
        user.find(kind=ui.input, content="Password (optional)").type("Passw0rd12345678")
        user.find("Finish setup and sign in", kind=ui.button).click()
        await user.should_see("That password is a well-known one", retries=SLOW)

        # mismatched confirmation
        user.find(kind=ui.input, content="Password (optional)").clear()
        user.find(kind=ui.input, content="Password (optional)").type(
            "long-enough-phrase-1"
        )
        user.find(kind=ui.input, content="Repeat password").type("different-phrase-11")
        user.find("Finish setup and sign in", kind=ui.button).click()
        await user.should_see("The two passwords don't match", retries=SLOW)

        # matching passwords: redeemed, welcomed by email, signed in
        user.find(kind=ui.input, content="Repeat password").clear()
        user.find(kind=ui.input, content="Repeat password").type("long-enough-phrase-1")
        user.find("Finish setup and sign in", kind=ui.button).click()
        await user.should_see("Find volunteers or teams…", retries=SLOW)
        _to, subject, body = await mail_to(sent, "new@example.org")
        assert subject == "Your VolunteerDB account is ready"
        assert "with your email and password" in body

        # the invite link is single-use
        await user.open(f"/invite/{token}")
        agree = next(
            cb for cb in user.find(kind=ui.checkbox).elements if "I agree" in cb.text
        )
        agree.value = True
        user.find("Finish setup and sign in", kind=ui.button).click()
        await user.should_see(
            "This link has expired or has already been used", retries=SLOW
        )
