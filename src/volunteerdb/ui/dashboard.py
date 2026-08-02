from urllib.parse import quote_plus

from nicegui import ui

from ..models import ROLE_LABELS
from ..services import graph as graph_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from .context import action_session, asof_banner, page_session, parse_as_of
from .cytoscape_element import CytoscapeGraph
from .layout import frame
from .volunteer_panel import VolunteerPanel


@ui.page("/")
async def dashboard(as_of: str = ""):
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
        my_assignments = (
            await volunteer_service.assignments(session, actor.volunteer_id, at=at)
            if actor.volunteer_id
            else []
        )
        # band chips in the legend, for the viewers who see coloured dots at all
        bands = (
            (await workload_service.get_config(session)).bands
            if actor.is_admin or actor.managed_team_ids
            else []
        )

    panel = VolunteerPanel(as_of)
    with frame("Dashboard", actor):
        asof_banner(at, "/")

        with ui.row().classes("items-center gap-2 w-full"):
            search = (
                ui.input("Find volunteers or teams…")
                .props("outlined dense clearable")
                .classes("w-72")
            )

            def go() -> None:
                ui.navigate.to(f"/volunteers?q={quote_plus(search.value or '')}")

            search.on("keydown.enter", go)
            ui.button("Search", on_click=go).props("dense")

        if my_assignments:
            ui.label("My teams").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for membership, team in my_assignments:
                    with (
                        ui.row()
                        .classes(
                            "items-center gap-2 p-2 rounded bg-blue-50 cursor-pointer w-full"
                        )
                        .on(
                            "click",
                            lambda _, tid=team.id: ui.navigate.to(f"/teams/{tid}"),
                        )
                    ):
                        ui.label(team.name).classes("font-medium")
                        ui.badge(ROLE_LABELS[membership.role])

        with ui.row().classes("items-center gap-2 w-full"):
            team_filter = (
                ui.select(team_options, label="Focus on team", value=0, with_input=True)
                .props("outlined dense")
                .classes("w-72")
            )

            async def refilter() -> None:
                async with action_session() as (session, actor):
                    new_elements = await graph_service.elements(
                        session, actor, team_id=team_filter.value or None, at=at
                    )
                graph.refresh(new_elements)

            team_filter.on_value_change(refilter)
            ui.button(icon="fit_screen", on_click=lambda: graph.fit()).props(
                "dense flat"
            ).tooltip("Fit the whole graph in view")
            ui.space()
            with ui.row().classes("items-center gap-3 flex-wrap"):
                _legend_entry("team", "background: var(--vdb-graph-team)")
                _legend_entry(
                    "volunteer", "background: var(--vdb-graph-node)", dot=True
                )
                _legend_entry(
                    "leadership", "background: var(--vdb-graph-leader)", edge=True
                )
                _legend_entry(
                    "sub-team", "background: var(--vdb-graph-hier)", edge=True
                )
                for band in bands:
                    _legend_entry(band.label, f"background: {band.color}", dot=True)

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


def _legend_entry(
    label: str, swatch_style: str, dot: bool = False, edge: bool = False
) -> None:
    classes = (
        "vdb-legend-edge"
        if edge
        else "vdb-legend-swatch" + (" vdb-legend-dot" if dot else "")
    )
    with ui.row().classes("items-center gap-1"):
        ui.element("span").classes(classes).style(swatch_style)
        ui.label(label).classes("text-xs text-gray-500")
