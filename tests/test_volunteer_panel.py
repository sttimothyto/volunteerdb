"""Headless UI test: the volunteer side panel opens from all three pages.

Single test function by design: page routes register when the ui modules are
first imported, which must happen inside the simulation's reset context (see
ui_sim_main.py) — a second simulation in the same process would find no pages.
"""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.ui.cytoscape_element import CytoscapeGraph

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_panel_opens_from_team_roster_table_and_graph(database):
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        maria = await volunteers.create(
            session, "Maria", "Alvarez", "maria@example.org", "555-1234"
        )
        await memberships.assign(session, maria.id, liturgy.id, TeamRole.leader)
        admin = await users.create(session, "admin@example.org", is_admin=True, password="pw")
        team_id, maria_id, admin_id = liturgy.id, maria.id, admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        # team roster: clicking a member's name fills and opens the panel
        await user.open(f"/teams/{team_id}")
        user.find("Maria Alvarez", kind=ui.label).trigger("click")
        await user.should_see("Email: maria@example.org")
        await user.should_see("Phone: 555-1234")
        await user.should_see("Serves on")

        # volunteers list: a table rowClick does the same
        await user.open("/volunteers")
        user.find(kind=ui.table).trigger("rowClick", args=[None, {"id": maria_id}, 0])
        await user.should_see("Email: maria@example.org")

        # graph: clicking a volunteer node does the same
        await user.open("/graph")
        user.find(kind=CytoscapeGraph).trigger(
            "node_click", args={"type": "volunteer", "volunteer_id": maria_id}
        )
        await user.should_see("Email: maria@example.org")
