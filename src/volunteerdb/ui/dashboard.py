from urllib.parse import quote_plus

from nicegui import ui

from .. import query_lang
from ..models import ROLE_LABELS
from ..services import graph as graph_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from .assets import static_url
from .context import action_session, page_session, parse_as_of
from .cytoscape_element import CytoscapeGraph
from .layout import frame
from .search_box import search_box
from .volunteer_panel import VolunteerPanel


@ui.page("/")
async def dashboard(as_of: str = ""):
    # the graph library is loaded via dynamic import() at Vue mount, far too
    # late for the browser's preload scanner — announce it in the head instead
    ui.add_head_html(
        f'<link rel="modulepreload" href="{static_url("cytoscape.esm.min.js")}">'
    )
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
    # a submitted WHERE filter narrows the graph in place; plain text still
    # navigates to the volunteers list like it always has
    active: dict = {"ids": None, "text": ""}

    async def refresh_graph() -> None:
        async with action_session() as (session, actor):
            new_elements = await graph_service.elements(
                session,
                actor,
                team_id=team_filter.value or None,
                at=at,
                volunteer_ids=active["ids"],
            )
        graph.refresh(new_elements)

    async def submit(text: str) -> None:
        if query_lang.parse(text) is None:
            ui.navigate.to(f"/volunteers?q={quote_plus(text)}")
            return
        try:
            async with action_session() as (session, actor):
                found = await volunteer_service.search_or_query(
                    session, text, at=at, include_inactive=actor.is_admin, actor=actor
                )
        except query_lang.QueryError as exc:
            ui.notify(str(exc), color="warning")
            return
        active["ids"] = {v.id for v in found}
        active["text"] = text
        render_chip()
        await refresh_graph()

    def render_chip() -> None:
        chip_row.clear()
        if active["ids"] is None:
            return

        async def remove() -> None:
            active["ids"] = None
            active["text"] = ""
            chip_row.clear()
            await refresh_graph()

        with chip_row:
            ui.chip(active["text"], removable=True, icon="filter_alt").mark(
                "graph-query-chip"
            ).on("remove", remove)

    with frame("Dashboard", actor, as_of=at, asof_path="/"):
        with ui.row().classes("items-center gap-2 w-full"):
            search_box(
                "Find volunteers or teams…",
                on_submit=submit,
                on_pick_volunteer=panel.open,
                at=at,
                as_of=as_of,
            )

        if my_assignments:
            ui.label("My teams").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for membership, team in my_assignments:
                    with (
                        ui.link(target=f"/teams/{team.id}").classes("w-full vdb-quiet"),
                        ui.row().classes(
                            "items-center gap-2 p-2 rounded bg-blue-50 cursor-pointer w-full"
                        ),
                    ):
                        ui.label(team.name).classes("font-medium")
                        ui.badge(ROLE_LABELS[membership.role])

        with ui.row().classes("items-center gap-2 w-full"):
            team_filter = (
                ui.select(team_options, label="Focus on team", value=0, with_input=True)
                .props("outlined dense")
                .classes("w-72")
            )

            team_filter.on_value_change(refresh_graph)
            ui.button(icon="fit_screen", on_click=lambda: graph.fit()).props(
                "dense flat"
            ).tooltip("Fit the whole graph in view")
            chip_row = ui.row().classes("items-center")
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
