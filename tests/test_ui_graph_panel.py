"""The graph is one click away (uiux-improvement.md, step 24).

The dashboard's ministry graph sits folded at the foot of the page and is
drawn on the first opening -- its rows read then, in their own unit of
work -- so a reader who came for the figures never pays for it. ?graph=
opens it with the page: a team id focuses it, "all" shows the parish.
"""

import asyncio

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
    ushers = ok(await teams.create(session, SYSTEM, "Ushers"))
    maria = ok(await volunteers.create(session, SYSTEM, "Maria", "Alvarez"))
    bruno = ok(await volunteers.create(session, SYSTEM, "Bruno", "Costa"))
    ok(await memberships.assign(session, SYSTEM, maria.id, music.id, TeamRole.member))
    ok(await memberships.assign(session, SYSTEM, bruno.id, ushers.id, TeamRole.member))
    admin, _ = ok(
        await users.create(
            session,
            "admin@example.org",
            is_admin=True,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    return {"music": music.id, "ushers": ushers.id, "admin_u": admin.id}


def _graphs(user) -> list:
    """The graphs on the page, none included (User.find asserts one)."""
    from volunteerdb.ui.cytoscape_element import CytoscapeGraph

    try:
        return list(user.find(kind=CytoscapeGraph).elements)
    except AssertionError:
        return []


async def test_the_graph_is_drawn_on_the_first_opening(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/")
        fold = only(user.find(marker="graph-panel"))
        assert fold.value is False, "folded until asked for"
        assert _graphs(user) == [], "nothing drawn, nothing read"
        await user.should_not_see("Focus on team")

        fold.value = True  # the click on its title
        for _ in range(SLOW):
            if _graphs(user):
                break
            await asyncio.sleep(0.1)
        graph = only(user.find(marker="graph-panel"))  # still the one panel
        assert graph.value is True
        (drawn,) = _graphs(user)
        node_ids = {n["data"]["id"] for n in drawn.props["elements"]["nodes"]}
        assert {f"t{ids['music']}", f"t{ids['ushers']}"} <= node_ids
        await user.should_see("Focus on team")
        await user.should_see("Tap a team to open its page")

        # closing and opening again draws nothing twice
        fold.value = False
        fold.value = True
        await asyncio.sleep(0.3)
        assert len(_graphs(user)) == 1


async def test_the_address_opens_the_panel_on_one_team(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/?graph={ids['music']}")
        assert only(user.find(marker="graph-panel")).value is True
        (drawn,) = _graphs(user)
        node_ids = {n["data"]["id"] for n in drawn.props["elements"]["nodes"]}
        assert f"t{ids['music']}" in node_ids and f"t{ids['ushers']}" not in node_ids
        focus = only(user.find(kind=ui.select, content="Focus on team"))
        assert focus.value == ids["music"]

        await user.open("/?graph=all")
        assert only(user.find(marker="graph-panel")).value is True
        (drawn,) = _graphs(user)
        node_ids = {n["data"]["id"] for n in drawn.props["elements"]["nodes"]}
        assert {f"t{ids['music']}", f"t{ids['ushers']}"} <= node_ids
