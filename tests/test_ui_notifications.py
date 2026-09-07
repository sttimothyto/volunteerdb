"""A success line that outlives the page reload it follows.

Most actions reload the page once they are done (ui/context.py, run_command),
and a toast sent just before that reload went down with the page -- which is
how the guide came to say "There is no message." context.flash stores the
line in the session and layout.frame shows it on the page that comes back.
The simulation's reload is a real re-open of the route, so the message has to
survive the same way it does in a browser."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok


async def _parish(session) -> dict[str, int]:
    music = ok(await teams.create(session, SYSTEM, "Music"))
    lena = ok(
        await volunteers.create(session, SYSTEM, "Lena", "Leader", "lena@example.org")
    )
    mia = ok(
        await volunteers.create(session, SYSTEM, "Mia", "Member", "mia@example.org")
    )
    ok(await memberships.assign(session, SYSTEM, lena.id, music.id, TeamRole.leader))
    lena_u, _ = ok(
        await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    return {"music": music.id, "mia": mia.id, "lena_u": lena_u.id}


async def test_a_reloading_action_still_says_it_worked(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        only(user.find(kind=ui.select, content="Volunteer")).set_value(ids["mia"])
        user.find(marker="add-member").click()
        # the page reloaded (Mia is on the roster) AND the line is there
        await user.should_see("Mia Member", retries=SLOW)
        await user.should_see("Added to the roster", retries=SLOW)

        # said once: the next page does not repeat it (the simulation keeps
        # every message ever shown, so the record is emptied first)
        user.notify.messages.clear()
        await user.open("/teams")
        await user.should_not_see("Added to the roster")


async def test_an_in_place_action_says_it_at_once(database):
    async with db_session() as session:
        ids = await _parish(session)
        ok(
            await memberships.assign(
                session, SYSTEM, ids["mia"], ids["music"], TeamRole.member
            )
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        # the role badge on Mia's row opens one small dialog
        row = next(
            r
            for r in only(user.find(marker="roster")).rows
            if r["name"] == "Mia Member"
        )
        user.find(marker="roster").trigger("role", row)
        await user.should_see("Change the role of Mia Member", retries=SLOW)
        only(user.find(marker="role-pick")).set_value("core")
        user.find(marker="role-save").click()
        await user.should_see("Role updated", retries=SLOW)
