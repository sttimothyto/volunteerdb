"""Headless UI test: the live suggestion dropdown on both search boxes.

Typing in the simulation assigns the input's value directly, which fires the
same on_value_change handler the browser's debounced update would, so the
lookups run without a browser (see nicegui.testing.user_interaction).
"""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

from tests.fp_helpers import ok

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

DASHBOARD_BOX = "Find volunteers or teams…"
LIST_BOX = "Search volunteers…"


async def test_dashboard_typeahead_suggests_teams_and_volunteers(database):
    async with db_session() as session:
        music = ok(await teams.create(session, None, "Music"))
        choir = ok(
            await teams.create(session, None, "Alvarado Choir", parent_team_id=music.id)
        )
        maria = await volunteers.create(
            session, None, "Maria", "Alvarez", "maria@example.org", "555-1234"
        )
        await memberships.assign(session, None, maria.id, choir.id, TeamRole.member)
        retired = await volunteers.create(session, None, "Pedro", "Alvarez")
        await volunteers.update(session, None, retired.id, is_active=False)
        await volunteers.create(session, None, "Bruno", "Costa")
        admin, _ = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        member, _ = await users.create(
            session, "member@example.org", password="test-pass-phrase"
        )
        maria_id, choir_id = maria.id, choir.id
        admin_id, member_id = admin.id, member.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        # a single character stays below the floor: no lookup, no dropdown
        await user.open("/")
        await user.should_see(DASHBOARD_BOX, retries=30)
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("A")
        await user.should_not_see("Maria Alvarez")

        # from two characters on, both categories are suggested
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("lv")
        await user.should_see("Maria Alvarez", retries=30)
        await user.should_see("Music / Alvarado Choir")  # full display path
        await user.should_see("Pedro Alvarez")  # admins see inactive rows...
        await user.should_see("inactive")  # ...badged as such
        await user.should_not_see("Bruno Costa")  # non-matching row stays out
        await user.should_see("See every match for “Alv”")

        # a fresh query replaces the previous suggestions rather than adding to them
        user.find(kind=ui.input, content=DASHBOARD_BOX).clear()
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Costa")
        await user.should_see("Bruno Costa", retries=30)
        await user.should_not_see("Maria Alvarez")

        # a query with no hits says so instead of leaving a stale list
        user.find(kind=ui.input, content=DASHBOARD_BOX).clear()
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("zzz")
        await user.should_see("Nothing found", retries=30)
        await user.should_not_see("Bruno Costa")

        # clicking a volunteer opens the side panel, as everywhere else
        user.find(kind=ui.input, content=DASHBOARD_BOX).clear()
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Alvarez")
        await user.should_see(f"suggest-volunteer-{maria_id}", retries=30)
        user.find(marker=f"suggest-volunteer-{maria_id}").click()
        await user.should_see("Email: maria@example.org", retries=30)

        # a team suggestion is a real link to its page (so right-click / new
        # tab work); the simulated user follows the href like a browser would
        await user.open("/")
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Choir")
        await user.should_see(f"suggest-team-{choir_id}", retries=30)
        item = user.find(marker=f"suggest-team-{choir_id}").elements.pop()
        assert item.props["href"] == f"/teams/{choir_id}"
        await user.open(item.props["href"])
        await user.should_see("Roster", retries=30)

        # Escape closes the dropdown; returning to a box that still holds the
        # query reopens it (retyping the same text is not a value change)
        await user.open("/")
        menu = user.find(marker="suggest-menu").elements.pop()
        assert not menu.value, "closed until there is something to suggest"
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Alv")
        await user.should_see(f"suggest-volunteer-{maria_id}", retries=30)
        assert menu.value, "suggestions opened the dropdown"
        user.find(kind=ui.input, content=DASHBOARD_BOX).trigger("keydown.esc")
        assert not menu.value
        user.find(kind=ui.input, content=DASHBOARD_BOX).trigger("click")
        assert menu.value, "clicking back into the box restores the suggestions"

        # a non-admin's suggestions leave the inactive volunteer out
        await user.open(f"/login-dev/{member_id}")
        await user.open("/")
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Alv")
        await user.should_see("Maria Alvarez", retries=30)
        await user.should_not_see("Pedro Alvarez")


async def test_volunteers_page_search_box_also_suggests(database):
    async with db_session() as session:
        maria = await volunteers.create(
            session, None, "Maria", "Alvarez", "maria@example.org"
        )
        admin, _ = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        maria_id, admin_id = maria.id, admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        await user.open("/volunteers")
        await user.should_see(LIST_BOX, retries=30)
        user.find(kind=ui.input, content=LIST_BOX).type("Alv")
        await user.should_see(f"suggest-volunteer-{maria_id}", retries=30)

        user.find(marker=f"suggest-volunteer-{maria_id}").click()
        await user.should_see("Email: maria@example.org", retries=30)


async def test_query_text_offers_run_and_filters_the_graph(database):
    from urllib.parse import quote_plus

    from volunteerdb.ui.cytoscape_element import CytoscapeGraph

    async with db_session() as session:
        music = ok(await teams.create(session, None, "Music"))
        maria = await volunteers.create(
            session, None, "Maria", "Alvarez", "maria@example.org", "555-1234"
        )
        bruno = await volunteers.create(
            session, None, "Bruno", "Costa", "bruno@example.org", "777-0000"
        )
        await memberships.assign(session, None, maria.id, music.id, TeamRole.member)
        await memberships.assign(session, None, bruno.id, music.id, TeamRole.member)
        admin, _ = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        bruno_u, _ = await users.create(
            session, "bruno@example.org", volunteer_id=bruno.id
        )
        maria_id, bruno_id = maria.id, bruno.id
        admin_id, bruno_uid = admin.id, bruno_u.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")
        await user.open("/")
        await user.should_see(DASHBOARD_BOX, retries=30)

        # query-shaped text offers to run the query instead of suggesting rows
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("first_name = 'Maria'")
        await user.should_see("Run query: first_name = 'Maria'", retries=30)
        await user.should_not_see("Maria Alvarez")

        # running it narrows the graph in place and pins a removable chip;
        # the chip renders before the refresh round-trip, so poll the graph
        async def graph_nodes() -> set[str]:
            import asyncio

            graph = user.find(kind=CytoscapeGraph).elements.pop()
            for _ in range(50):
                ids = {n["data"]["id"] for n in graph._props["elements"]["nodes"]}
                if ids != full_nodes:
                    return ids
                await asyncio.sleep(0.1)
            return {n["data"]["id"] for n in graph._props["elements"]["nodes"]}

        full_nodes = {
            n["data"]["id"]
            for n in user.find(kind=CytoscapeGraph)
            .elements.pop()
            ._props["elements"]["nodes"]
        }
        user.find(marker="suggest-query").click()
        await user.should_see("graph-query-chip", retries=30)
        node_ids = await graph_nodes()
        assert f"v{maria_id}" in node_ids and f"v{bruno_id}" not in node_ids

        # removing the chip restores the whole graph
        full_nodes = node_ids
        user.find(marker="graph-query-chip").trigger("remove")
        await user.should_not_see("graph-query-chip")
        node_ids = await graph_nodes()
        assert f"v{bruno_id}" in node_ids

        # a typo'd field warns instead of quietly falling back to substring
        user.find(kind=ui.input, content=DASHBOARD_BOX).clear()
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("bogus = 1")
        await user.should_see("Run query: bogus = 1", retries=30)
        user.find(marker="suggest-query").click()
        await user.should_see("unknown field: bogus", retries=30)

        # the volunteers page runs the same queries via its q param
        def names() -> list[str]:
            table = user.find(kind=ui.table).elements.pop()
            return [r["name"] for r in table.rows]

        await user.open("/volunteers?q=" + quote_plus("phone LIKE '555%'"))
        await user.should_see(LIST_BOX, retries=30)
        assert names() == ["Maria Alvarez"]
        await user.open("/volunteers?q=" + quote_plus("bogus = 1"))
        await user.should_see("unknown field: bogus", retries=30)

        # a member's private-field query is scoped to their own row
        await user.open(f"/login-dev/{bruno_uid}")
        await user.open("/volunteers?q=" + quote_plus("phone LIKE '7%'"))
        await user.should_see(LIST_BOX, retries=30)
        assert names() == ["Bruno Costa"]
        await user.open("/volunteers?q=" + quote_plus("phone LIKE '555%'"))
        await user.should_see(LIST_BOX, retries=30)
        assert names() == [], "another volunteer's phone match stays invisible"
