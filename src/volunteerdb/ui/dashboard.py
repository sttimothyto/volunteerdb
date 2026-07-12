from nicegui import ui

from ..models import ROLE_LABELS, TeamRole
from ..services import reports as report_service
from ..services import volunteers as volunteer_service
from .context import page_session
from .layout import frame


@ui.page("/")
async def dashboard():
    async with page_session() as (session, actor):
        show_coverage = actor.is_admin or bool(actor.managed_team_ids)
        coverage = await report_service.coverage(session) if show_coverage else []
        if not actor.is_admin:
            coverage = [r for r in coverage if r.team.id in actor.managed_team_ids]
        my_assignments = (
            await volunteer_service.assignments(session, actor.volunteer_id)
            if actor.volunteer_id
            else []
        )

    with frame("Dashboard", actor):
        with ui.row().classes("items-center gap-2 w-full"):
            search = ui.input("Find a volunteer…").props("outlined dense clearable").classes("w-72")
            search.on(
                "keydown.enter", lambda: ui.navigate.to(f"/volunteers?q={search.value or ''}")
            )
            ui.button(
                "Search", on_click=lambda: ui.navigate.to(f"/volunteers?q={search.value or ''}")
            ).props("dense")

        if show_coverage:
            holes = [r for r in coverage if r.missing_leader or r.missing_second]
            ui.label("Holes to fill").classes("text-lg font-medium mt-2")
            if not holes:
                ui.label("Every team has a leader and a second-in-command. 🎉").classes(
                    "text-positive"
                )
            with ui.column().classes("w-full gap-1"):
                for r in holes:
                    with ui.row().classes(
                        "w-full items-center gap-2 p-2 rounded bg-red-50 cursor-pointer"
                    ).on("click", lambda _, tid=r.team.id: ui.navigate.to(f"/teams/{tid}")):
                        ui.label(r.path).classes("font-medium")
                        if r.missing_leader:
                            ui.badge("no leader", color="negative")
                        if r.missing_second:
                            ui.badge("no second-in-command", color="warning")
                        ui.space()
                        ui.label(f"{r.total} member{'s' if r.total != 1 else ''}").classes(
                            "text-sm text-gray-600"
                        )

            ui.label("All teams").classes("text-lg font-medium mt-4")
            columns = [
                {"name": "path", "label": "Team", "field": "path", "align": "left", "sortable": True},
                {"name": "leader", "label": ROLE_LABELS[TeamRole.leader], "field": "leader"},
                {"name": "second", "label": ROLE_LABELS[TeamRole.second], "field": "second"},
                {"name": "core", "label": ROLE_LABELS[TeamRole.core], "field": "core"},
                {"name": "member", "label": ROLE_LABELS[TeamRole.member], "field": "member"},
                {"name": "total", "label": "Total", "field": "total", "sortable": True},
            ]
            rows = [
                {
                    "id": r.team.id,
                    "path": r.path,
                    "leader": r.counts.get(TeamRole.leader, 0),
                    "second": r.counts.get(TeamRole.second, 0),
                    "core": r.counts.get(TeamRole.core, 0),
                    "member": r.counts.get(TeamRole.member, 0),
                    "total": r.total,
                }
                for r in coverage
            ]
            table = ui.table(columns=columns, rows=rows, row_key="id", pagination=15).classes(
                "w-full"
            )
            table.on("rowClick", lambda e: ui.navigate.to(f"/teams/{e.args[1]['id']}"))

        if my_assignments:
            ui.label("My teams").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for membership, team in my_assignments:
                    with ui.row().classes(
                        "items-center gap-2 p-2 rounded bg-blue-50 cursor-pointer w-full"
                    ).on("click", lambda _, tid=team.id: ui.navigate.to(f"/teams/{tid}")):
                        ui.label(team.name).classes("font-medium")
                        ui.badge(ROLE_LABELS[membership.role])
