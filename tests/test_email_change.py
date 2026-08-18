"""Changing the address an account signs in with.

The address is not contact data here: on the passwordless path it *is* the
credential, since a one-time code mailed to it opens the account. So a new one
is a claim until somebody reads mail there, and the claim is settled by a
link — not by whoever happens to be typing.

What the service must never do is move anything early. Every test below checks
the address on file after the request as well as after the confirmation. The
last two cover the other page a volunteer can type an address on — their own
profile — and the asymmetry there: your address waits, everyone else's does
not.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import mail, memberships, teams, users, volunteers

from .conftest import SLOW, mail_to

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def _volunteer_with_account(session, first="Maria", addr="maria@example.org"):
    volunteer = await volunteers.create(session, first, "Alvarez", email=addr)
    account = await users.create(session, addr, volunteer_id=volunteer.id)
    return volunteer, account


async def test_requesting_moves_nothing_and_confirming_moves_both(database):
    async with db_session() as session:
        volunteer, account = await _volunteer_with_account(session)
        volunteer_id, user_id = volunteer.id, account.id

    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "Maria.New@Example.ORG "
        )

    async with db_session() as session:
        account = await users.get(session, user_id)
        assert account.email == "maria@example.org", "the login address has not moved"
        assert account.pending_email == "maria.new@example.org", "trimmed, folded"
        assert (await volunteers.get(session, volunteer_id)).email == (
            "maria@example.org"
        ), "nor has the roster address"

    async with db_session() as session:
        confirmed = await users.confirm_email_change(session, token)
        assert confirmed is not None

    async with db_session() as session:
        account = await users.get(session, user_id)
        assert account.email == "maria.new@example.org"
        assert account.pending_email is None
        assert account.email_change_token is None
        assert account.email_change_expires_at is None
        # the cascade: one person, one address, so every team that reads
        # volunteer.email — rosters, event notices, the Drive export — follows
        assert (await volunteers.get(session, volunteer_id)).email == (
            "maria.new@example.org"
        )


async def test_the_address_reaches_every_active_membership(database):
    """Not by writing to membership — there is nothing there to write — but
    because every audience is computed from volunteer.email at send time."""
    async with db_session() as session:
        volunteer, account = await _volunteer_with_account(session)
        for name in ("Liturgy", "Hospitality"):
            team = await teams.create(session, name)
            await memberships.assign(session, volunteer.id, team.id, TeamRole.member)
        user_id = account.id

    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "moved@example.org"
        )
    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is not None

    async with db_session() as session:
        volunteer = (await volunteers.search(session, "Alvarez"))[0]
        rows = await volunteers.assignments(session, volunteer.id)
        assert len(rows) == 2
        assert volunteer.email == "moved@example.org"


async def test_a_link_works_once(database):
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "once@example.org"
        )
    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is not None
    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is None


async def test_an_expired_link_is_refused_exactly_like_an_unknown_one(database):
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "late@example.org"
        )
        account = await users.get(session, user_id)
        account.email_change_expires_at = datetime.now(UTC) - timedelta(minutes=1)

    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is None
        assert await users.confirm_email_change(session, "no-such-token") is None
        assert await users.confirm_email_change(session, "") is None
        assert (await users.get(session, user_id)).email == "maria@example.org"


async def test_asking_again_replaces_the_pending_address_and_kills_the_old_link(
    database,
):
    """A typo is corrected by retyping, not by waiting a day."""
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        _user, typo = await users.start_email_change(
            session, user_id, "typo@example.org"
        )
    async with db_session() as session:
        _user, good = await users.start_email_change(
            session, user_id, "good@example.org"
        )

    async with db_session() as session:
        assert await users.confirm_email_change(session, typo) is None
    async with db_session() as session:
        assert await users.confirm_email_change(session, good) is not None
        assert (await users.get(session, user_id)).email == "good@example.org"


async def test_cancelling_kills_the_link(database):
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "second.thoughts@example.org"
        )
    async with db_session() as session:
        await users.cancel_email_change(session, user_id)
    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is None
        assert (await users.get(session, user_id)).pending_email is None


async def test_an_address_another_account_signs_in_with_is_refused_up_front(database):
    """Refused before any mail goes out: a confirmation that could never work
    would only tell a stranger their address is in the database."""
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        await users.create(session, "taken@example.org")
        user_id = account.id

    async with db_session() as session:
        with pytest.raises(ValueError, match="another account"):
            await users.start_email_change(session, user_id, "taken@example.org")
        assert (await users.get(session, user_id)).pending_email is None


async def test_an_address_claimed_between_request_and_confirmation_is_refused(database):
    """Two people may ask for the same address; only the first to confirm gets
    it. pending_email is deliberately not unique, so this is settled here."""
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "contested@example.org"
        )
    async with db_session() as session:
        await users.create(session, "contested@example.org")

    async with db_session() as session:
        with pytest.raises(ValueError, match="taken"):
            await users.confirm_email_change(session, token)
    async with db_session() as session:
        account = await users.get(session, user_id)
        assert account.email == "maria@example.org", "nothing moved"
        assert account.pending_email is None, "and the dead link is cleared away"


async def test_the_address_on_file_is_refused_as_a_change(database):
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        with pytest.raises(ValueError, match="already the address"):
            await users.start_email_change(session, user_id, " MARIA@example.org ")


async def test_something_that_is_not_an_address_is_refused(database):
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        for bad in ("", "   ", "not-an-address", "two words@example.org"):
            with pytest.raises(ValueError, match="not an email address"):
                await users.start_email_change(session, user_id, bad)


async def test_a_code_in_flight_dies_with_the_address_that_earned_it(database):
    """The one-time code proved control of the *old* mailbox. That identifier
    is gone, so the code must not be spendable against the new one."""
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        started = await users.start_otp_login(session, "maria@example.org")
        assert started is not None and started[1] is not None
        code = started[1]
        _user, token = await users.start_email_change(
            session, user_id, "fresh@example.org"
        )
    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is not None
    async with db_session() as session:
        account = await users.get(session, user_id)
        assert account.otp_hash is None
        assert await users.verify_otp(session, "fresh@example.org", code) is None


async def test_a_deactivated_account_cannot_confirm(database):
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "ghost@example.org"
        )
        await users.set_flags(session, user_id, is_active=False)
    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is None


async def test_looking_at_a_link_does_not_spend_it(database):
    """Mail scanners and link prefetchers open URLs on their own; the page
    reads the pending address with this, and only the button redeems it."""
    async with db_session() as session:
        _v, account = await _volunteer_with_account(session)
        user_id = account.id
    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "curious@example.org"
        )

    async with db_session() as session:
        peeked = await users.pending_email_change(session, token)
        assert peeked is not None and peeked.pending_email == "curious@example.org"
        assert await users.pending_email_change(session, "nonsense") is None
    async with db_session() as session:
        assert (await users.get(session, user_id)).email == "maria@example.org"
        assert await users.confirm_email_change(session, token) is not None


async def test_an_account_with_no_volunteer_record_still_changes_its_login(database):
    async with db_session() as session:
        account = await users.create(session, "admin@example.org", is_admin=True)
        user_id = account.id
    async with db_session() as session:
        _user, token = await users.start_email_change(
            session, user_id, "office@example.org"
        )
    async with db_session() as session:
        assert await users.confirm_email_change(session, token) is not None
        assert (await users.get(session, user_id)).email == "office@example.org"


# --- the other place a volunteer can type their own address ---


async def test_editing_your_own_profile_defers_the_address_but_saves_the_rest(
    database, monkeypatch
):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> bool:
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(mail, "send_email", fake_send)

    async with db_session() as session:
        volunteer, account = await _volunteer_with_account(session)
        volunteer_id, user_id = volunteer.id, account.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{user_id}")
        await user.open(f"/volunteers/{volunteer_id}")
        user.find("Edit", kind=ui.button).click()
        await user.should_see("Edit Maria Alvarez", retries=SLOW)
        await user.should_see("sends a confirmation link")

        user.find(marker="edit-email").clear()
        user.find(marker="edit-email").type("maria.moved@example.org")
        user.find(kind=ui.input, content="Phone").type("555-0100")
        user.find("Save", kind=ui.button).click()
        await user.should_see("Confirmation sent", retries=SLOW)
        await mail_to(sent, "maria.moved@example.org")

    async with db_session() as session:
        volunteer = await volunteers.get(session, volunteer_id)
        assert volunteer.email == "maria@example.org", "the address waits"
        assert volunteer.phone == "555-0100", "everything else saved as usual"
        assert (
            await users.get(session, user_id)
        ).pending_email == "maria.moved@example.org"


async def test_a_leader_correcting_someone_elses_address_applies_at_once(
    database, monkeypatch
):
    """The deliberate asymmetry: a leader fixes a bounced address precisely
    because the volunteer cannot read their mail, so waiting on them to
    confirm would make the correction impossible."""
    monkeypatch.setattr(mail, "send_email", fake_ok)

    async with db_session() as session:
        team = await teams.create(session, "Liturgy")
        member = await volunteers.create(
            session, "Felix", "Garcia", email="typo@example.org"
        )
        await memberships.assign(session, member.id, team.id, TeamRole.member)
        lena = await volunteers.create(session, "Lena", "Leader")
        await memberships.assign(session, lena.id, team.id, TeamRole.leader)
        leader_account = await users.create(
            session, "lena@example.org", volunteer_id=lena.id
        )
        member_id, leader_user_id = member.id, leader_account.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{leader_user_id}")
        await user.open(f"/volunteers/{member_id}")
        user.find("Edit", kind=ui.button).click()
        await user.should_see("Edit Felix Garcia", retries=SLOW)
        await user.should_not_see("sends a confirmation link")

        user.find(marker="edit-email").clear()
        user.find(marker="edit-email").type("felix@example.org")
        user.find("Save", kind=ui.button).click()
        # the profile behind the dialog redraws with the corrected address
        await user.should_see("Email: felix@example.org", retries=SLOW)

    async with db_session() as session:
        assert (await volunteers.get(session, member_id)).email == "felix@example.org"


async def fake_ok(*_args, **_kwargs) -> bool:
    return True
