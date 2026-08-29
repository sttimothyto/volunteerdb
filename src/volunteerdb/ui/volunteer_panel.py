"""Slide-in right drawer with a compact volunteer profile.

Shared by the volunteers list, team roster, and ministry graph pages. Drawers
must be direct children of the page content, so create the panel *before*
entering ``frame`` and call ``panel.open(volunteer_id)`` from click handlers.
"""

from nicegui import ui

from ..models import ROLE_LABELS, CustomFieldDef, FieldType
from ..permissions import volunteer_team_ids
from ..services import custom_fields as custom_field_service
from ..services import photos as photo_service
from ..services import teams as team_service
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from . import invites
from .a11y import icon_button
from .account_status import invitable, last_login_text
from .context import page_ctx, parse_as_of
from .photo_dialog import photo_avatar


def format_custom(defn: CustomFieldDef, value, missing: str = "—") -> str:
    if value is None:
        return missing
    match FieldType(defn.field_type):
        case FieldType.checkbox:
            return "yes" if value else "no"
        case FieldType.timestamp | FieldType.timestamptz:
            return str(value).replace("T", " ")
        case _:
            return str(value)


def volunteer_link(
    name: str, volunteer_id: int, panel: "VolunteerPanel", *, classes: str = ""
) -> ui.label:
    """A volunteer's name, clickable to open the side panel beside it.

    The idiom lived inline in the team roster until the events pages needed it
    in four more places. ui.label has no on_click parameter, so the generic
    .on() is the way (same as photo_dialog.photo_avatar)."""
    return (
        ui.label(name)
        .classes(
            f"font-medium cursor-pointer text-primary hover:underline {classes}".rstrip()
        )
        .on("click", lambda _, vid=volunteer_id: panel.open(vid))
    )


class VolunteerPanel:
    def __init__(self, as_of: str = "", base_url: str = "") -> None:
        self.at = parse_as_of(as_of)
        self._asof_query = f"?as_of={as_of}" if as_of else ""
        # only needed to build an invite link; without it the panel simply
        # reports sign-in status without offering to fix it
        self.base_url = base_url
        self.drawer = ui.right_drawer(value=False, fixed=True, bordered=True).props(
            "overlay"
        )
        self.drawer._props["width"] = 380
        with self.drawer:
            self.content = ui.column().classes("w-full gap-1")

    async def open(self, volunteer_id: int) -> None:
        async with page_ctx() as ctx:
            session, actor = ctx.session, ctx.actor
            volunteer = await volunteer_service.get(session, volunteer_id, at=self.at)
        if volunteer is None:
            ui.notify(
                f"No volunteer with id {volunteer_id} at this time.",
                color="warning",
            )
            return
        async with page_ctx() as ctx:
            session, actor = ctx.session, ctx.actor
            team_ids = await volunteer_team_ids(session, volunteer_id)
            can_view = actor.can_view_volunteer(volunteer_id, team_ids)
            can_edit = actor.can_edit_volunteer(volunteer_id, team_ids)
            field_defs = await custom_field_service.list_defs(session)
            wl = await workload_service.visible_scores(
                session, actor, {volunteer_id: team_ids}, at=self.at
            )
            assignments = await volunteer_service.assignments(
                session, volunteer_id, at=self.at
            )
            paths = (await team_service.tree(session, at=self.at)).paths
            photo_at = (await photo_service.versions(session, [volunteer_id])).get(
                volunteer_id
            )
            account = await user_service.account_for_volunteer(session, volunteer_id)
            can_invite = (
                actor.can_invite_volunteer(team_ids)
                and self.at is None
                and volunteer.is_active
                and self.base_url != ""
            )
            # only an admin is shown the link itself (ui/invites.py)
            reveal_invite = actor.is_admin

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
                icon_button("close", "Close", on_click=self.drawer.hide).props(
                    "flat dense round"
                )
            if not volunteer.is_active or volunteer_id in wl:
                with ui.row().classes("items-center gap-2"):
                    if not volunteer.is_active:
                        ui.badge("inactive", color="muted")
                    if volunteer_id in wl:
                        score, band = wl[volunteer_id]
                        ui.badge(f"workload: {band.label} · {float(score):g}").style(
                            f"background-color: {band.color}; "
                            f"color: {workload_service.text_colour(band.color)}"
                        ).tooltip(
                            "Workload score: team weights × role multipliers, all ministries"
                        )
            if can_view:
                ui.label(f"Email: {volunteer.email or '—'}").classes(
                    "text-sm text-gray-700"
                )
                ui.label(f"Phone: {volunteer.phone or '—'}").classes(
                    "text-sm text-gray-700"
                )
                for defn in field_defs:
                    value = (volunteer.custom or {}).get(defn.key)
                    ui.label(f"{defn.label}: {format_custom(defn, value)}").classes(
                        "text-sm text-gray-700"
                    )
                if can_edit and volunteer.notes:
                    ui.label(f"Notes: {volunteer.notes}").classes(
                        "text-sm text-gray-700"
                    )
            else:
                ui.label(
                    "Contact details visible to their team leaders and core members."
                ).classes("text-sm text-gray-400 italic")

            # outside the can_view gate, like the profile page's Last login line
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.label(f"Last login: {last_login_text(account)}").classes(
                    "text-sm text-gray-700"
                )
                if can_invite and invitable(account):
                    invites.invite_control(
                        volunteer_id,
                        volunteer.full_name,
                        volunteer.email,
                        account,
                        self.base_url,
                        reveal=reveal_invite,
                        where="detail",
                    )

            ui.label("Serves on").classes("font-medium mt-2")
            if not assignments:
                ui.label("Not on any team.").classes("text-sm text-gray-500")
            for membership, team in assignments:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.link(
                        paths.get(team.id, team.name),
                        f"/teams/{team.id}{self._asof_query}",
                    ).classes("text-sm")
                    ui.badge(ROLE_LABELS[membership.role])

            ui.button("Full profile", icon="open_in_new").props(
                # the detail page is live-only now: no as-of query to carry over
                f'dense outline href="/volunteers/{volunteer_id}"'
            ).classes("mt-3")
        self.drawer.show()
