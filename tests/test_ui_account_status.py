"""Sign-in status on the profile page and in the team roster.

The point of these tests is the *absence* of a gate. Last login and the
has-an-account badge render for anyone who can already see the person — a
plain team member reading their own roster included — so the interesting
viewer here is Mia, who sees names and nothing else.
"""

from datetime import UTC, datetime
from pathlib import Path

from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

from tests.fp_helpers import ok

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

# late morning in Toronto, so the rendered date is the same day in any
# plausible display timezone
LOGGED_IN_AT = datetime(2026, 3, 1, 16, 0, tzinfo=UTC)


async def _parish(session) -> dict[str, int]:
    """Music (under Liturgy, which Lena leads) with one member of each kind:
    Mia never signed in, Nils has no account, Opal has, Quin's is switched off.

    Note that users.create with no password arms an invite link, so Mia — who
    has never used hers — reads as "invite sent" rather than "account". Opal is
    given a password to make her the plain settled case."""
    liturgy = ok(await teams.create(session, None, "Liturgy"))
    music = ok(await teams.create(session, None, "Music", parent_team_id=liturgy.id))

    lena = await volunteers.create(session, None, "Lena", "Leader", "lena@example.org")
    mia = await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
    nils = await volunteers.create(session, None, "Nils", "Nobody", "nils@example.org")
    opal = await volunteers.create(session, None, "Opal", "Online", "opal@example.org")
    quin = await volunteers.create(session, None, "Quin", "Quiet", "quin@example.org")
    ok(await memberships.assign(session, None, lena.id, liturgy.id, TeamRole.leader))
    for v in (mia, nils, opal, quin):
        ok(await memberships.assign(session, None, v.id, music.id, TeamRole.member))

    lena_u, _ = await users.create(session, "lena@example.org", volunteer_id=lena.id)
    mia_u, _ = await users.create(session, "mia@example.org", volunteer_id=mia.id)
    opal_u, _ = await users.create(session, "opal@example.org", volunteer_id=opal.id)
    quin_u, _ = await users.create(session, "quin@example.org", volunteer_id=quin.id)
    opal_u.last_login_at = LOGGED_IN_AT
    opal_u.password_hash = "x"  # settled: signed in and chose a password
    quin_u.last_login_at = LOGGED_IN_AT
    quin_u.is_active = False
    await session.flush()

    return {
        "music": music.id,
        "mia": mia.id,
        "nils": nils.id,
        "opal": opal.id,
        "quin": quin.id,
        "mia_u": mia_u.id,
        "lena_u": lena_u.id,
    }


async def test_roster_shows_who_has_an_account_to_plain_members(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Roster")
        # Mia is a plain member: names but no contact details...
        await user.should_not_see("opal@example.org")
        # ...and yet the whole account column
        await user.should_see("no account")  # Nils
        await user.should_see("invite sent")  # Mia herself, link still unused
        await user.should_see("never signed in")  # and the line beneath it
        await user.should_see("account")  # Opal, settled
        await user.should_see("last login 2026-03-01")  # Opal
        await user.should_see("disabled")  # Quin

        # reporting only: a plain member is offered no way to act on any of it
        await user.should_not_see("invite to create account")
        await user.should_not_see("send a new invite")


async def test_last_login_shows_on_a_profile_the_viewer_cannot_read(database):
    """Mia may not see Nils' or Opal's contact details; she still sees whether
    and when they signed in."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")

        await user.open(f"/volunteers/{ids['nils']}")
        await user.should_see("Contact details visible to their team leaders")
        await user.should_see("Last login: no VolunteerDB account")

        await user.open(f"/volunteers/{ids['opal']}")
        await user.should_see("Last login: 2026-03-01")

        await user.open(f"/volunteers/{ids['quin']}")
        await user.should_see("account disabled")

        await user.open(f"/volunteers/{ids['mia']}")
        await user.should_see("Last login: never signed in")
