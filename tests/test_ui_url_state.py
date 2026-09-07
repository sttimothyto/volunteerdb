"""Listings carry their state in the URL (uiux-improvement.md, step 23).

/volunteers already navigated with ?q= and ?band=. /teams and /events now
read ?q= into their search box and narrow the table on open, and write it
back as the reader types (tables.in_address, a history.replaceState the
browser test in tests/e2e/test_browser_search.py exercises). The other
controls on /events keep it in their links.

No page module is imported here: NiceGUI's simulation re-imports each one
per run (testing/general.py pops them from sys.modules), and a copy imported
at collection would leave the next run without its routes.
"""

import asyncio

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import teams, users

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok
from tests.test_ui_events import _parish, _seed_event


async def _until(holds) -> None:
    for _ in range(SLOW):
        if holds():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the page never navigated")


async def test_teams_open_narrowed_to_the_address(database):
    async with db_session() as session:
        liturgy = ok(await teams.create(session, SYSTEM, "Liturgy"))
        ok(await teams.create(session, SYSTEM, "Music", parent_team_id=liturgy.id))
        ok(await teams.create(session, SYSTEM, "Hospitality"))
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        admin_id = admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/teams?q=music")
        box = only(user.find(kind=ui.input, content="Search teams…"))
        assert box.value == "music"
        table = only(user.find(kind=ui.table))
        assert [r["name"] for r in table.rows] == ["Liturgy", "Music"]
        await user.should_see("2 of 3 teams")
        assert len(table.every) == 3, "the whole parish is behind the box"


async def test_events_open_narrowed_to_the_address(database):
    async with db_session() as session:
        ids = await _parish(session)
    await _seed_event(ids["liturgy"], title="Sunday Mass")
    await _seed_event(ids["liturgy"], title="Choir practice")

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/events?q=choir")
        box = only(user.find(kind=ui.input, content="Search events…"))
        assert box.value == "choir"
        table = only(user.find(kind=ui.table))
        assert [r["title"] for r in table.rows] == ["Choir practice"]
        await user.should_see("1 of 2 events")
        assert len(table.every) == 2, "the whole list is behind the box"

        # the other controls keep the search in their links
        user.find("Show past", kind=ui.button).click()
        await _until(lambda: "past=1" in user.back_history[-1])
        assert user.back_history[-1].endswith("&q=choir")
