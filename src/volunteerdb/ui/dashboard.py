from urllib.parse import quote_plus

from fastapi import Request
from nicegui import ui

from .. import query_lang
from ..models import ROLE_LABELS
from ..services import graph as graph_service
from ..services import stats as stats_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from .assets import static_url
from .context import action_session, page_session, parse_as_of
from .cytoscape_element import CytoscapeGraph
from .layout import frame
from .search_box import search_box
from .stat_tiles import chip_row, stat_chip, stat_section, stat_tile, tile_row
from .volunteer_panel import VolunteerPanel

# what a snapshot cannot answer, said once
AS_OF_NOTE = (
    "Counts are as of the snapshot; what is happening now — shifts, "
    "elections and sign-ins — is left out."
)


@ui.page("/")
async def dashboard(request: Request, as_of: str = ""):
    # the graph library is loaded via dynamic import() at Vue mount, far too
    # late for the browser's preload scanner — announce it in the head instead
    ui.add_head_html(
        f'<link rel="modulepreload" href="{static_url("cytoscape.esm.min.js")}">'
    )
    at = parse_as_of(as_of)
    async with page_session() as (session, actor):
        elements = await graph_service.elements(session, actor, at=at)
        tree = await team_service.tree(session, at=at)
        paths = tree.paths
        team_options = {0: "— whole parish —"} | {
            t.id: paths[t.id]
            for t in tree.teams
            if actor.is_admin or actor.can_view_roster_names(t.id)
        }
        my_assignments = (
            await volunteer_service.assignments(session, actor.volunteer_id, at=at)
            if actor.volunteer_id
            else []
        )
        figures = await stats_service.dashboard(session, actor, at=at)
        # band chips in the legend, for the viewers who see coloured dots at all
        bands = (
            (await workload_service.get_config(session)).bands
            if actor.is_admin or actor.managed_team_ids
            else []
        )

    panel = VolunteerPanel(as_of, str(request.base_url).rstrip("/"))
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
        chip_holder.clear()
        if active["ids"] is None:
            return

        async def remove() -> None:
            active["ids"] = None
            active["text"] = ""
            chip_holder.clear()
            await refresh_graph()

        with chip_holder:
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

        # Statistics run widest-audience first — the parish, then what the
        # people who run ministries must act on — and then narrow to the
        # reader: their teams, then their own service. All four bands sit
        # above the graph, which is the exploratory tail rather than the
        # answer most readers came for. Each block is absent, not empty, for a
        # viewer without the right to it; the service never ran its queries.
        if figures.parish is not None:
            _parish_section(figures.parish, live=figures.live)
        if figures.leadership is not None:
            _leadership_section(
                figures.leadership, live=figures.live, is_admin=actor.is_admin
            )
        if my_assignments:
            _my_teams_section(my_assignments, as_of=as_of)
        if figures.personal is not None:
            _my_service_section(figures.personal)

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
            chip_holder = ui.row().classes("items-center")
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
            "Click a team to open its page; click a volunteer to open their side "
            "panel. Zoom in to read names, or hover a node to isolate its "
            "connections."
        ).classes("text-sm text-gray-400")


def _parish_section(p: stats_service.ParishStats, *, live: bool) -> None:
    with stat_section("Parish", None if live else AS_OF_NOTE):
        with tile_row():
            stat_tile(
                p.active_volunteers,
                "Active volunteers",
                sub=f"{p.inactive_volunteers} inactive"
                if p.inactive_volunteers
                else None,
                href="/volunteers",
            )
            stat_tile(p.active_teams, "Active teams", href="/teams")
            stat_tile(
                p.assignments,
                "Assignments",
                sub=f"{p.ministries_per_volunteer} per volunteer",
                hint="One person on three teams counts three times.",
            )
            stat_tile(
                p.unassigned_volunteers,
                "On no team",
                href="/volunteers",
                warn=bool(p.unassigned_volunteers),
                hint="Active volunteers holding no membership at all.",
            )
            if p.accounts is not None:
                stat_tile(
                    p.accounts,
                    "Can sign in",
                    sub=f"of {p.active_volunteers} volunteers",
                    href="/admin/users",
                )


def _leadership_section(
    lead: stats_service.LeadershipStats, *, live: bool, is_admin: bool
) -> None:
    with stat_section("Needs attention", None if live else AS_OF_NOTE):
        with tile_row():
            # an admin's scope is the whole parish, so these two would only
            # repeat the section above; for everyone else they are the size
            # of what they are responsible for, and belong here
            if not is_admin:
                stat_tile(lead.teams, "Teams I help run", href="/teams")
                stat_tile(lead.people, "People on them", href="/volunteers")
            stat_tile(
                lead.people_without_email,
                "No email address",
                warn=bool(lead.people_without_email),
                hint="They cannot be invited to an account or emailed about an event.",
                href="/volunteers",
            )
            if lead.teams_without_leader is not None:
                stat_tile(
                    lead.teams_without_leader,
                    "Without a leader",
                    warn=bool(lead.teams_without_leader),
                    href="/teams",
                )
                stat_tile(
                    lead.teams_without_second,
                    "Without a second",
                    warn=bool(lead.teams_without_second),
                    href="/teams",
                )
            if lead.understaffed_events is not None:
                stat_tile(
                    lead.understaffed_events,
                    "Shifts short of people",
                    sub="next 30 days",
                    warn=bool(lead.understaffed_events),
                    href="/events",
                )
        if lead.gap_teams:
            with chip_row("Gaps:"):
                for gap in lead.gap_teams:
                    missing = " and ".join(
                        f"no {what}"
                        for what, absent in (
                            ("leader", gap.missing_leader),
                            ("second", gap.missing_second),
                        )
                        if absent
                    )
                    ui.link(f"{gap.path} — {missing}", f"/teams/{gap.team_id}").classes(
                        "text-sm"
                    )
        if lead.bands:
            with chip_row("Workload:"):
                for band in lead.bands:
                    stat_chip(
                        band.label,
                        band.count,
                        color=band.color,
                        href=f"/volunteers?band={band.label}",
                    )
        if lead.open_elections:
            with chip_row("Open seats:"):
                for phase in lead.open_elections:
                    stat_chip(phase.label, phase.count, href="/elections")


def _my_teams_section(assignments: list, *, as_of: str) -> None:
    """The reader's own memberships — its own band now, not a footnote
    under My service. For most people this is the whole reason they
    opened the page, so it comes before the figures and the graph."""
    with stat_section("My teams"):
        with ui.column().classes("w-full gap-1"):
            for membership, team in assignments:
                suffix = f"?as_of={as_of}" if as_of else ""
                with (
                    ui.link(target=f"/teams/{team.id}{suffix}").classes(
                        "w-full vdb-quiet"
                    ),
                    ui.row().classes(
                        "items-center gap-2 p-2 rounded bg-blue-50 cursor-pointer w-full"
                    ),
                ):
                    ui.label(team.name).classes("font-medium")
                    ui.badge(ROLE_LABELS[membership.role])


def _my_service_section(mine: stats_service.PersonalStats) -> None:
    """What the reader has done and is owed. Never their workload band —
    nobody sees their own."""
    with stat_section("My service"):
        with tile_row():
            stat_tile(
                mine.upcoming_duties,
                "Upcoming duties",
                sub=mine.next_duty_at.astimezone().strftime("next %-d %b, %H:%M")
                if mine.next_duty_at
                else None,
                hint=f"{mine.next_duty_title} · {mine.next_duty_slot}"
                if mine.next_duty_title
                else None,
                href="/events",
            )
            if mine.claimable_subs:
                stat_tile(
                    mine.claimable_subs,
                    "Shifts I could cover",
                    href="/events",
                )
            if mine.ballots_waiting:
                stat_tile(
                    mine.ballots_waiting,
                    "Ballots waiting",
                    warn=True,
                    href="/elections",
                )
            stat_tile(
                # Decimal("3.00") formats as "3.00" under :g; via float it
                # reads as "3", and 3.5 still reads as "3.5"
                f"{float(mine.hours_served):g}",
                "Hours served",
                sub=f"{mine.events_attended} event"
                f"{'' if mine.events_attended == 1 else 's'} attended",
            )


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
