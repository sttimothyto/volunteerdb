"""A list with nothing in it says so, and offers the next thing.

widgets.empty_state: the fact ("Nobody matches “xyz”."), and a button for
what comes next -- clear the search, add the first member, create the first
event. A blank page with "0 volunteers" under it left the reader guessing
whether the search, the band filter or the parish was empty."""

from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


async def _parish(session) -> dict[str, int]:
    """Music (Lena leads, alone on it) and Choir (empty); an admin."""
    music = ok(await teams.create(session, SYSTEM, "Music"))
    choir = ok(await teams.create(session, SYSTEM, "Choir"))
    lena = ok(
        await volunteers.create(session, SYSTEM, "Lena", "Leader", "lena@example.org")
    )
    ok(await memberships.assign(session, SYSTEM, lena.id, music.id, TeamRole.leader))
    ok(await memberships.assign(session, SYSTEM, lena.id, choir.id, TeamRole.leader))
    admin, _ = ok(
        await users.create(
            session,
            "admin@example.org",
            is_admin=True,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
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
    return {
        "music": music.id,
        "choir": choir.id,
        "admin_u": admin.id,
        "lena_u": lena_u.id,
    }


async def test_a_search_with_no_hits_names_it_and_offers_to_clear(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/volunteers?q=zzz")
        await user.should_see("Nobody matches “zzz”.")
        clear = only(user.find(marker="empty-action"))
        assert clear.text == "Clear search" and clear.props["href"] == "/volunteers"

        # the band filter is a chip on the page, not only a value in a box
        await user.open("/volunteers?q=zzz&band=green")
        await user.should_see("Nobody in the green band matches “zzz”.")
        await user.should_see(marker="band-chip")
        await user.should_see("Workload: green")

        await user.open("/volunteers")
        await user.should_not_see(marker="empty-state")


async def test_an_empty_events_list_keeps_the_search_box_and_offers_new_event(
    database,
):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/events")
        await user.should_see("Nothing scheduled yet.")
        await user.should_see("Search events…")
        assert only(user.find(marker="empty-action")).text == "New event"
        user.find(marker="empty-action").click()
        await user.should_see("Repeat weekly until")  # the New event dialog

        await user.open("/events?past=1")
        await user.should_see("No past events yet.")
        await user.should_not_see(marker="empty-action")


async def test_an_empty_roster_offers_the_first_member(database):
    async with db_session() as session:
        ids = await _parish(session)
        # Choir: Lena leads it from Music's side? No -- she leads it directly,
        # so her own row would be on it. Take her off to empty the roster.
        for m, team in await volunteers.assignments(session, 1):
            if team.id == ids["choir"]:
                ok(await memberships.remove(session, SYSTEM, m.id))

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/teams/{ids['choir']}")
        await user.should_see("Nobody on this team yet.")
        assert only(user.find(marker="empty-action")).text == "Add the first member"


async def test_a_leader_with_no_election_open_is_told_where_to_start(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/elections")
        await user.should_see("No election is open on your teams.")
        await user.should_see("Start one from a vacancy below.")  # Music wants a second
        await user.should_see("Vacancies")
