from nicegui import ui

from ..services import graph as graph_service
from ..services import teams as team_service
from .context import action_session, asof_banner, page_session, parse_as_of
from .layout import frame
from .cytoscape_element import CytoscapeGraph
from .volunteer_panel import VolunteerPanel


@ui.page("/graph")
async def graph_page(as_of: str = ""):
    at = parse_as_of(as_of)
    async with page_session() as (session, actor):
        elements = await graph_service.elements(session, actor, at=at)
        all_teams = await team_service.list_all(session, at=at)
        paths = team_service.team_paths(all_teams)
        team_options = {0: "— whole parish —"} | {
            t.id: paths[t.id]
            for t in all_teams
            if actor.is_admin or actor.can_view_roster_names(t.id)
        }

    panel = VolunteerPanel(as_of)
    with frame("Ministry graph", actor):
        asof_banner(at, "/graph")

        with ui.row().classes("items-center gap-2 w-full"):
            team_filter = ui.select(
                team_options, label="Focus on team", value=0, with_input=True
            ).props("outlined dense").classes("w-72")

            async def refilter() -> None:
                async with action_session() as (session, actor):
                    new_elements = await graph_service.elements(
                        session, actor, team_id=team_filter.value or None, at=at
                    )
                graph.refresh(new_elements)

            team_filter.on_value_change(refilter)
            ui.space()
            ui.label(
                "teams = terracotta boxes · volunteers = dots, coloured by capacity for "
                "admins · gold = leadership"
            ).classes("text-xs text-gray-500")

        async def on_node_click(e) -> None:
            data = e.args
            if data.get("type") == "volunteer":
                await panel.open(data["volunteer_id"])
                return
            suffix = f"?as_of={as_of}" if as_of else ""
            ui.navigate.to(f"/teams/{data['team_id']}{suffix}")

        graph = CytoscapeGraph(elements, on_node_click=on_node_click).classes(
            "w-full border rounded"
        )
        ui.label(
            "Click a team to open its page; click a volunteer to open their side panel."
        ).classes("text-sm text-gray-400")
