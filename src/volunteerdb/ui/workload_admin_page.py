"""Admin screen for workload: role multipliers, colour band thresholds, team weights."""

from decimal import Decimal

from nicegui import ui

from ..models import ROLE_LABELS, TeamRole
from ..permissions import require
from ..services import teams as team_service
from ..services import workload as workload_service
from .context import action_session, notify_errors, page_session
from .layout import frame


@ui.page("/admin/workload")
async def workload_page():
    async with page_session() as (session, actor):
        if not actor.is_admin:
            with frame("Workload", actor):
                ui.label("Admins only.").classes("text-gray-500")
            return
        config = await workload_service.get_config(session)
        all_teams = await team_service.list_all(session)
        paths = team_service.team_paths(all_teams)

    with frame("Workload", actor):
        ui.label(
            "A volunteer's workload score is the sum, over every team they serve on, of the "
            "team's workload weight × their role's multiplier. Bands colour-code the score on "
            "the volunteers list and the graph. Visible to admins and to the "
            "leaders/seconds of a volunteer's teams; configured here by admins only."
        ).classes("text-sm text-gray-500 vdb-prose")

        with ui.card().classes("w-full gap-2 p-4"):
            ui.label("Role multipliers").classes("text-lg font-medium")
            multiplier_inputs: dict[TeamRole, ui.number] = {}
            with ui.row().classes("gap-4"):
                for role in TeamRole:
                    multiplier_inputs[role] = (
                        ui.number(
                            ROLE_LABELS[role],
                            value=float(config.multipliers[role]),
                            min=0,
                            step=0.5,
                        )
                        .props("outlined dense")
                        .classes("w-40")
                    )

            ui.label("Colour bands").classes("text-lg font-medium")
            band_rows: list[tuple[ui.input, ui.color_input, ui.number | None]] = []
            for i, b in enumerate(config.bands):
                is_last = i == len(config.bands) - 1
                with ui.row().classes("items-center gap-3"):
                    label = (
                        ui.input("Label", value=b.label)
                        .props("outlined dense")
                        .classes("w-32")
                    )
                    color = (
                        ui.color_input(label="Colour", value=b.color)
                        .props("dense")
                        .classes("w-36")
                    )
                    if is_last:
                        upper = None
                        ui.label("everything above").classes("text-sm text-gray-500")
                    else:
                        upper = (
                            ui.number(
                                "up to score", value=float(b.upper), min=0, step=0.5
                            )
                            .props("outlined dense")
                            .classes("w-32")
                        )
                    band_rows.append((label, color, upper))

            @notify_errors
            async def save_config() -> None:
                new_config = workload_service.WorkloadConfig(
                    multipliers={
                        role: Decimal(str(inp.value or 0))
                        for role, inp in multiplier_inputs.items()
                    },
                    bands=[
                        workload_service.Band(
                            (label.value or "").strip(),
                            color.value or "#9e9e9e",
                            None if upper is None else Decimal(str(upper.value or 0)),
                        )
                        for label, color, upper in band_rows
                    ],
                )
                async with action_session() as (session, actor):
                    require(actor.is_admin, "only admins configure workload")
                    await workload_service.set_config(session, actor, new_config)
                ui.notify("Workload settings saved", color="positive")

            ui.button("Save settings", icon="save", on_click=save_config).props("dense")

        with ui.card().classes("w-full gap-2 p-4"):
            ui.label("Team workload weights").classes("text-lg font-medium")
            ui.label(
                "Optional per-ministry weight; empty teams don't count towards anyone's score. "
                "Also editable on each team's edit dialog."
            ).classes("text-sm text-gray-500 vdb-prose")
            weight_inputs: dict[int, ui.number] = {}
            originals: dict[int, Decimal | None] = {}
            for team in sorted(all_teams, key=lambda t: paths[t.id].lower()):
                with ui.row().classes("w-full items-center gap-3"):
                    ui.label(paths[team.id]).classes("w-96")
                    originals[team.id] = team.workload_weight
                    weight_inputs[team.id] = (
                        ui.number(
                            value=None
                            if team.workload_weight is None
                            else float(team.workload_weight),
                            min=0,
                            step=0.5,
                        )
                        .props("outlined dense clearable")
                        .classes("w-32")
                    )

            @notify_errors
            async def save_weights() -> None:
                changed = 0
                async with action_session() as (session, actor):
                    require(actor.is_admin, "only admins set workload weights")
                    for team_id, inp in weight_inputs.items():
                        new = None if inp.value is None else Decimal(str(inp.value))
                        if new != originals[team_id]:
                            await team_service.update(
                                session, actor, team_id, workload_weight=new
                            )
                            changed += 1
                ui.notify(
                    f"Updated {changed} team weight{'s' if changed != 1 else ''}",
                    color="positive",
                )

            ui.button("Save weights", icon="save", on_click=save_weights).props("dense")
