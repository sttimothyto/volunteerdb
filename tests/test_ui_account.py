"""Self-service sign-in settings on /account: the password, and the address.

The interesting rule is who has to re-type the old password: a session that
signed in *with* it does, a session that signed in with an emailed code does
not — that second case is what makes "I forgot my password" self-serviceable
without an admin (NIST SP 800-63B §4.1.2.1 counts it as binding a new
authenticator, not account recovery).
"""

import re
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import mail, memberships, teams, users, volunteers

from .conftest import SLOW, mail_to
from tests import mint
from tests.fakes import SIM_MAILER
from tests.fp_helpers import ok

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_otp_session_sets_a_password_without_the_old_one(database, monkeypatch):
    SIM_MAILER.sent.clear()
    sent = SIM_MAILER.sent

    async with db_session() as session:
        user, _ = ok(
            await users.create(
                session,
                "forgetful@example.org",
                password="cedar lamp figs",
                invite=mint.fresh_invite(),
            )
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
                session,
                "forgetful@example.org",
                "thistle brook lantern",
                now=mint.now(),
            )
        ).is_ok()
        assert (
            await users.authenticate(
                session, "forgetful@example.org", "cedar lamp figs", now=mint.now()
            )
        ).is_err(), "the old password is gone"

    # §4.1.2: the account's address hears about it, through a channel the
    # browser doing the change does not control
    _to, subject, _body = await mail_to(sent, "forgetful@example.org")
    assert subject == "Your VolunteerDB password changed"


async def test_password_session_must_retype_the_current_password(database, monkeypatch):
    monkeypatch.setattr(mail, "send_email", lambda *a, **k: _ok())

    async with db_session() as session:
        user, _ = ok(
            await users.create(
                session,
                "careful@example.org",
                password="cedar lamp figs",
                invite=mint.fresh_invite(),
            )
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
                session,
                "careful@example.org",
                "thistle brook lantern",
                now=mint.now(),
            )
        ).is_ok()


async def _ok() -> bool:
    return True


async def test_changing_your_own_address_waits_for_the_new_one_to_confirm(
    database, monkeypatch
):
    """The whole flow at the surface: ask on /account, nothing moves, open the
    link that lands in the new mailbox, and both addresses move together."""
    SIM_MAILER.sent.clear()
    sent = SIM_MAILER.sent

    async with db_session() as session:
        maria = ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", email="maria@example.org"
            )
        )
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        ok(
            await memberships.assign(
                session, None, maria.id, liturgy.id, TeamRole.leader
            )
        )
        account, _ = ok(
            await users.create(
                session,
                "maria@example.org",
                volunteer_id=maria.id,
                invite=mint.fresh_invite(),
            )
        )
        volunteer_id, user_id = maria.id, account.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{user_id}")
        await user.open("/account")
        await user.should_see("Change your email address")

        user.find(marker="new-email").type("maria.new@example.org")
        user.find("Send confirmation", kind=ui.button).click()
        await user.should_see("Confirmation sent", retries=SLOW)

        to, subject, body = await mail_to(sent, "maria@example.org")
        assert subject == "Your VolunteerDB address is being changed", (
            "the address being replaced is warned while it can still say no "
            "(SP 800-63B §4.1.2: a channel the browser cannot suppress)"
        )
        assert "maria.new@example.org" in body and "/account" in body, (
            "it names the incoming address and where to cancel"
        )

        proof = [m for m in sent if m[0] == "maria.new@example.org"]
        assert len(proof) == 1
        _to, subject, body = proof[0]
        assert subject == "Confirm your new VolunteerDB address"
        assert "/confirm-email/" in body, "the link goes only to the new address"
        link = re.search(r"/confirm-email/(\S+)", body).group(1)

        # nothing has moved yet
        async with db_session() as session:
            assert (await users.get(session, user_id)).email == "maria@example.org"
            assert (
                await volunteers.get(session, volunteer_id)
            ).email == "maria@example.org"

        # the page shows the pending change on the way past
        await user.open("/account")
        await user.should_see("Waiting for maria.new@example.org to confirm")

        # opening the link only offers; the button spends it
        await user.open(f"/confirm-email/{link}")
        await user.should_see("Confirm your new address")
        async with db_session() as session:
            assert (await users.get(session, user_id)).email == "maria@example.org", (
                "a mail scanner following the link must not burn it"
            )

        user.find("Confirm this address", kind=ui.button).click()
        await user.should_see("Address confirmed", retries=SLOW)

        # the outgoing mailbox gets the receipt, and the last word
        _to, subject, body = await mail_to(sent, "maria@example.org")
        assert subject == "Your VolunteerDB address changed"
        assert "maria.new@example.org" in body
        assert "last message" in body

    async with db_session() as session:
        account = await users.get(session, user_id)
        assert account.email == "maria.new@example.org", "the sign-in address moved"
        assert account.pending_email is None
        # ...and with it the address every team they serve on reads
        assert (
            await volunteers.get(session, volunteer_id)
        ).email == "maria.new@example.org"


async def test_a_pending_address_change_can_be_called_off(database, monkeypatch):
    monkeypatch.setattr(mail, "send_email", lambda *a, **k: _ok())

    async with db_session() as session:
        user, _ = ok(
            await users.create(
                session, "unsure@example.org", invite=mint.fresh_invite()
            )
        )
        user_id = user.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{user_id}")
        await user.open("/account")
        user.find(marker="new-email").type("elsewhere@example.org")
        user.find("Send confirmation", kind=ui.button).click()
        await user.should_see("Confirmation sent", retries=SLOW)

        await user.open("/account")
        user.find("Cancel", kind=ui.button).click()
        await user.should_see("cancelled", retries=SLOW)

    async with db_session() as session:
        assert (await users.get(session, user_id)).pending_email is None


async def test_the_address_change_form_refuses_a_typo_and_a_taken_address(
    database, monkeypatch
):
    monkeypatch.setattr(mail, "send_email", lambda *a, **k: _ok())

    async with db_session() as session:
        user, _ = ok(
            await users.create(
                session, "hopeful@example.org", invite=mint.fresh_invite()
            )
        )
        ok(
            await users.create(
                session, "spoken.for@example.org", invite=mint.fresh_invite()
            )
        )
        user_id = user.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{user_id}")
        await user.open("/account")

        user.find(marker="new-email").type("not-an-address")
        user.find("Send confirmation", kind=ui.button).click()
        await user.should_see("not an email address", retries=SLOW)

        user.find(marker="new-email").clear()
        user.find(marker="new-email").type("spoken.for@example.org")
        user.find("Send confirmation", kind=ui.button).click()
        await user.should_see("another account", retries=SLOW)

    async with db_session() as session:
        assert (await users.get(session, user_id)).pending_email is None
