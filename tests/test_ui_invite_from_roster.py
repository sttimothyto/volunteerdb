"""Inviting a roster member to create an account, from the team page.

The point of these tests is who gets the button and who does not. Sign-in status
itself is shown to everyone (test_ui_account_status.py); the *control* belongs to
the people who run the ministry — leaders, seconds and core members — and to
nobody else, on no snapshot, and for nobody there is no address to write to.

Markers rather than text for every click: /teams/{id} renders the roster and the
volunteer drawer on one page, and NiceGUI's should_see finds text inside a closed
drawer, so asserting on labels alone would pass on things no human can reach.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import mail, memberships, teams, users, volunteers

from .conftest import SLOW, mail_to

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str, str]]:
    """Capture outbound mail. mail.send_email is looked up as a module
    attribute at call time, which is what makes this patch land."""
    captured: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> bool:
        captured.append((to, subject, body))
        return True

    monkeypatch.setattr(mail, "send_email", fake_send)
    return captured


async def _parish(session) -> dict[str, int]:
    """Liturgy (Lena leads, Cora is core) > Music, whose members cover every
    state the control has to deal with: Nils has no account, Stale's invite ran
    out unused, Live's is still good, Void has no email address at all."""
    liturgy = await teams.create(session, "Liturgy")
    music = await teams.create(session, "Music", parent_team_id=liturgy.id)

    lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
    cora = await volunteers.create(session, "Cora", "Core", "cora@example.org")
    await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
    await memberships.assign(session, cora.id, liturgy.id, TeamRole.core)

    nils = await volunteers.create(session, "Nils", "Nobody", "nils@example.org")
    stale = await volunteers.create(session, "Stale", "Sender", "stale@example.org")
    livev = await volunteers.create(session, "Live", "Link", "live@example.org")
    void = await volunteers.create(session, "Void", "Nomail")  # no address
    mia = await volunteers.create(session, "Mia", "Member", "mia@example.org")
    for v in (nils, stale, livev, void, mia):
        await memberships.assign(session, v.id, music.id, TeamRole.member)

    stale_u, _ = await users.invite_volunteer(session, stale.id)
    stale_u.invite_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await users.invite_volunteer(session, livev.id)

    # actor accounts: password written straight in, so no argon2 pass is spent
    accounts = {}
    for name, v in (("lena", lena), ("cora", cora), ("mia", mia)):
        u = await users.create(session, f"{name}@example.org", volunteer_id=v.id)
        u.password_hash = "x"  # never verified; /login-dev establishes the session
        accounts[name] = u
    await session.flush()

    return {
        "music": music.id,
        "nils": nils.id,
        "stale": stale.id,
        "live": livev.id,
        "void": void.id,
        "lena_u": accounts["lena"].id,
        "cora_u": accounts["cora"].id,
        "mia_u": accounts["mia"].id,
    }


async def test_leader_invites_a_member_and_the_row_catches_up(database, sent):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("no account")  # Nils, at rest

        user.find(marker=f"invite-roster-{ids['nils']}").click()
        await user.should_see("Send an invite to Nils Nobody?", retries=SLOW)
        await user.should_see("nils@example.org")  # the dialog names the address

        user.find(marker="invite-confirm").click()
        await user.should_see("Invite link for nils@example.org", retries=SLOW)

        to, subject, body = await mail_to(sent, "nils@example.org")
        assert "VolunteerDB account" in subject
        assert "/invite/" in body, "the email carries the redemption link"

    async with db_session() as session:
        account = await users.account_for_volunteer(session, ids["nils"])
        assert account is not None, "the account exists now"
        assert account.password_hash is None, "they choose their own"
        assert not account.is_admin
        assert users.invite_live(account)
        assert body.count(account.invite_token) == 1, "and it is *their* token"


async def test_cancelling_the_confirmation_creates_nothing(database, sent):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        user.find(marker=f"invite-roster-{ids['nils']}").click()
        await user.should_see("Send an invite to Nils Nobody?", retries=SLOW)
        user.find(marker="invite-cancel").click()
        await user.should_see("Roster")

    assert sent == [], "nobody was emailed"
    async with db_session() as session:
        assert await users.account_for_volunteer(session, ids["nils"]) is None


async def test_core_may_invite_and_a_plain_member_may_not(database, sent):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open(f"/teams/{ids['music']}")
        # a core member reads the whole roster and may close its gaps
        await user.should_see(marker=f"invite-roster-{ids['nils']}")

        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("no account")  # she still sees the status...
        await user.should_not_see("invite to create account")  # ...not the action
        await user.should_not_see(marker=f"invite-roster-{ids['nils']}")


async def test_a_lapsed_invite_may_be_resent_but_a_live_one_is_not_reoffered(
    database, sent
):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")

        await user.should_see("invite expired")  # Stale
        # a link nobody used may be replaced — there is no password to lose
        await user.should_see(marker=f"invite-roster-{ids['stale']}")
        # a live invite reports itself and offers the link, not a blind resend
        await user.should_see("invite sent")  # Live
        user.find(marker=f"invite-roster-{ids['live']}").click()
        await user.should_see("Invite link for live@example.org", retries=SLOW)
        assert sent == [], "reopening the dialog sends nothing on its own"


async def test_a_volunteer_with_no_email_gets_no_control(database, sent):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        # there is nowhere to send a link
        await user.should_not_see(marker=f"invite-roster-{ids['void']}")


async def test_a_snapshot_reports_but_never_invites(database, sent):
    """The accounts map is deliberately live on a historical roster, so without
    the as-of gate a leader could invite somebody who left years ago."""
    async with db_session() as session:
        ids = await _parish(session)
    today = datetime.now(UTC).date().isoformat()

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}?as_of={today}")
        await user.should_see("Read-only snapshot")
        await user.should_see("no account")  # the badge still reports
        await user.should_not_see(marker=f"invite-roster-{ids['nils']}")


async def test_the_profile_page_offers_the_same_control(database, sent):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/volunteers/{ids['nils']}")
        await user.should_see("Last login: no VolunteerDB account")

        user.find(marker=f"invite-profile-{ids['nils']}").click()
        await user.should_see("Send an invite to Nils Nobody?", retries=SLOW)
        user.find(marker="invite-confirm").click()
        await user.should_see("Invite link for nils@example.org", retries=SLOW)
        await mail_to(sent, "nils@example.org")
