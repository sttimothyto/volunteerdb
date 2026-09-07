"""A refusal has a way back.

widgets.denied draws the sentence and a button to a page the reader can
read. "Admins only." and "This event is visible to the members of its
team." used to end there, leaving a member with the browser's back button
and nothing else on the page."""

from datetime import timedelta

from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import events as event_service
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


async def _parish(session) -> dict[str, int]:
    """Choir, with an event; Mia is on Music and nowhere near it."""
    music = ok(await teams.create(session, SYSTEM, "Music"))
    choir = ok(await teams.create(session, SYSTEM, "Choir"))
    mia = ok(
        await volunteers.create(session, SYSTEM, "Mia", "Member", "mia@example.org")
    )
    ok(await memberships.assign(session, SYSTEM, mia.id, music.id, TeamRole.member))
    mia_u, _ = ok(
        await users.create(
            session,
            "mia@example.org",
            volunteer_id=mia.id,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    starts = mint.now().replace(hour=15, minute=0) + timedelta(days=7)
    created = ok(
        await event_service.create_event(
            session,
            SYSTEM,
            team_id=choir.id,
            title="Choir practice",
            starts_at=starts,
            ends_at=starts + timedelta(hours=2),
            created_by=None,
            tz=mint.tz(),
            series_id=mint.uuid(),
        )
    )
    return {"choir": choir.id, "mia_u": mia_u.id, "event": created[0].id}


def _back(user) -> tuple[str, str]:
    button = only(user.find(marker="denied-back"))
    return button.text, button.props["href"]


async def test_a_member_on_an_admin_page_is_sent_home(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        for path in ("/admin/users", "/admin/fields", "/admin/workload"):
            await user.open(path)
            await user.should_see("Admins only.")
            assert _back(user) == ("Dashboard", "/")

        await user.open("/elections")
        await user.should_see("Elections are available to admins")
        assert _back(user) == ("Dashboard", "/")


async def test_another_teams_event_and_a_missing_page_point_at_the_listing(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{ids['event']}")
        await user.should_see("This event is visible to the members of its team.")
        assert _back(user) == ("Events", "/events")

        await user.open("/events/99999")
        await user.should_see("No event with id 99999.")
        assert _back(user) == ("Events", "/events")

        await user.open("/volunteers/99999")
        await user.should_see("No volunteer with id 99999.")
        assert _back(user) == ("Volunteers", "/volunteers")

        # a team page she is not on: the roster is a refusal with a way back
        await user.open(f"/teams/{ids['choir']}")
        await user.should_see("its roster is not visible to you")
        assert _back(user) == ("Teams", "/teams")
