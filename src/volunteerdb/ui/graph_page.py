from nicegui import ui

from ..services import graph as graph_service
from ..services import teams as team_service
from .context import action_session, asof_banner, page_session, parse_as_of
from .layout import frame
from .cytoscape_element import CytoscapeGraph


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
            ui.label("teams = blue boxes · volunteers = dots · orange = leadership").classes(
                "text-xs text-gray-500"
            )

        def on_node_click(e) -> None:
            data = e.args
            detail_card.clear()
            with detail_card:
                with ui.row().classes("items-center gap-2"):
                    if data.get("type") == "team":
                        ui.icon("groups")
                        ui.label(data.get("path", data.get("label", ""))).classes("font-medium")
                        ui.button(
                            "Open team",
                            on_click=lambda: ui.navigate.to(f"/teams/{data['team_id']}"),
                        ).props("dense outline")
                    else:
                        ui.icon("person")
                        ui.label(data.get("label", "")).classes("font-medium")
                        ui.button(
                            "Open volunteer",
                            on_click=lambda: ui.navigate.to(f"/volunteers/{data['volunteer_id']}"),
                        ).props("dense outline")

        graph = CytoscapeGraph(elements, on_node_click=on_node_click).classes(
            "w-full border rounded"
        )
        detail_card = ui.row().classes("w-full min-h-10 items-center")
        with detail_card:
            ui.label("Click a node to see details.").classes("text-sm text-gray-400")
