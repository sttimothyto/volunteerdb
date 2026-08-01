"""Slide-in right drawer with a compact volunteer profile.

Shared by the volunteers list, team roster, and ministry graph pages. Drawers
must be direct children of the page content, so create the panel *before*
entering ``frame`` and call ``panel.open(volunteer_id)`` from click handlers.
"""

from nicegui import ui

from ..models import ROLE_LABELS, CustomFieldDef, FieldType
from ..permissions import volunteer_team_ids
from ..services import workload as workload_service
from ..services import custom_fields as custom_field_service
from ..services import photos as photo_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from .context import action_session, notify_errors, parse_as_of
from .photo_dialog import photo_avatar


def format_custom(defn: CustomFieldDef, value, missing: str = "—") -> str:
    if value is None:
        return missing
    if defn.field_type == FieldType.checkbox.value:
        return "yes" if value else "no"
    return str(value)


class VolunteerPanel:
    def __init__(self, as_of: str = "") -> None:
        self.at = parse_as_of(as_of)
        self._asof_query = f"?as_of={as_of}" if as_of else ""
        self.drawer = ui.right_drawer(value=False, fixed=True, bordered=True).props("overlay")
        self.drawer._props["width"] = 380
        with self.drawer:
            self.content = ui.column().classes("w-full gap-1")

    @notify_errors
    async def open(self, volunteer_id: int) -> None:
        async with action_session() as (session, actor):
            volunteer = await volunteer_service.get(session, volunteer_id, at=self.at)
            if volunteer is None:
                ui.notify(f"No volunteer with id {volunteer_id} at this time.", color="warning")
                return
            team_ids = await volunteer_team_ids(session, volunteer_id)
            can_view = actor.can_view_volunteer(volunteer_id, team_ids)
            can_edit = actor.can_edit_volunteer(volunteer_id, team_ids)
            field_defs = await custom_field_service.list_defs(session)
            wl = await workload_service.visible_scores(
                session, actor, {volunteer_id: team_ids}, at=self.at
            )
            assignments = await volunteer_service.assignments(session, volunteer_id, at=self.at)
            paths = team_service.team_paths(await team_service.list_all(session, at=self.at))
            photo_at = (await photo_service.versions(session, [volunteer_id])).get(volunteer_id)

        self.content.clear()
        with self.content:
            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                # photos are current-state only, so as-of snapshots render read-only
                photo_avatar(
                    volunteer_id,
                    volunteer.full_name,
                    photo_at,
                    on_change=(
                        (lambda: self.open(volunteer_id)) if self.at is None else None
                    ),
                )
                ui.label(volunteer.full_name).classes("text-lg font-medium")
                ui.space()
                ui.button(icon="close", on_click=self.drawer.hide).props("flat dense round")
            if not volunteer.is_active or volunteer_id in wl:
                with ui.row().classes("items-center gap-2"):
                    if not volunteer.is_active:
                        ui.badge("inactive", color="grey")
                    if volunteer_id in wl:
                        score, band = wl[volunteer_id]
                        ui.badge(f"workload: {band.label} · {float(score):g}").style(
                            f"background-color: {band.color}"
                        ).tooltip(
                            "Workload score: team weights × role multipliers, all ministries"
                        )
            if can_view:
                ui.label(f"Email: {volunteer.email or '—'}").classes("text-sm text-gray-700")
                ui.label(f"Phone: {volunteer.phone or '—'}").classes("text-sm text-gray-700")
                for defn in field_defs:
                    value = (volunteer.custom or {}).get(defn.key)
                    ui.label(f"{defn.label}: {format_custom(defn, value)}").classes(
                        "text-sm text-gray-700"
                    )
                if can_edit and volunteer.notes:
                    ui.label(f"Notes: {volunteer.notes}").classes("text-sm text-gray-700")
            else:
                ui.label("Contact details visible to their team leaders and core members.").classes(
                    "text-sm text-gray-400 italic"
                )

            ui.label("Serves on").classes("font-medium mt-2")
            if not assignments:
                ui.label("Not on any team.").classes("text-sm text-gray-500")
            for membership, team in assignments:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.link(
                        paths.get(team.id, team.name), f"/teams/{team.id}{self._asof_query}"
                    ).classes("text-sm")
                    ui.badge(ROLE_LABELS[membership.role])

            ui.button(
                "Full profile",
                icon="open_in_new",
                # the detail page is live-only now: no as-of query to carry over
                on_click=lambda: ui.navigate.to(f"/volunteers/{volunteer_id}"),
            ).props("dense outline").classes("mt-3")
        self.drawer.show()
