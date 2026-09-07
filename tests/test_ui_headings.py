"""Section titles are headings.

A screen reader lists a page's headings to get around it (WCAG 2.4.6): the
page title is the h1, every section under it an h2, a card or sub-section
an h3. The source sweep in test_ui_css_invariants forbids the look-alike;
this reads the tree of two pages and the sign-in card, which had no h1."""

from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.ui.a11y import Heading

from tests import mint
from tests.conftest import SIM_MAIN, db_session
from tests.fp_helpers import ok


def _headings(user) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    for h in user.find(kind=Heading).elements:
        found.setdefault(int(h.tag[1]), []).append(h.text)
    return found


async def test_a_team_page_reads_as_an_outline(database):
    async with db_session() as session:
        music = ok(await teams.create(session, SYSTEM, "Music"))
        lena = ok(
            await volunteers.create(
                session, SYSTEM, "Lena", "Leader", "lena@example.org"
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, lena.id, music.id, TeamRole.leader
            )
        )
        lena_u, _ = ok(
            await users.create(
                session,
                "lena@example.org",
                volunteer_id=lena.id,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{lena_u.id}")
        await user.open(f"/teams/{music.id}")
        found = _headings(user)
        assert found[1] == ["Music"], "one h1: the page title"
        assert {"Add member", "Roster", "Roster spreadsheet"} <= set(found[2])
        assert "Import a .csv" in found[3], "a sub-section is an h3"

        await user.open("/account")
        found = _headings(user)
        assert found[1] == ["Your account"]
        assert {"Change your email address", "Your duties in your own calendar"} <= set(
            found[2]
        )


async def test_the_sign_in_card_has_an_h1(database):
    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open("/login")
        assert _headings(user)[1] == ["Volunteer Database (VDB)"]
