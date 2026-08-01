from urllib.parse import quote_plus

from nicegui import app, ui

from ..models import ROLE_LABELS, CustomFieldDef, FieldType, TeamRole
from ..permissions import require, team_ids_map, volunteer_team_ids
from ..services import custom_fields as custom_field_service
from ..services import memberships as membership_service
from ..services import photos as photo_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from .context import action_session, notify_errors, page_session
from .layout import frame
from .photo_dialog import photo_avatar
from .timeline_chart import timeline_chart
from .volunteer_panel import VolunteerPanel, format_custom

ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}


@ui.page("/volunteers")
async def volunteers_page(q: str = "", band: str = ""):
    async with page_session() as (session, actor):
        found = await volunteer_service.search(
            session, q, include_inactive=actor.is_admin, actor=actor
        )
        team_hits = await team_service.search(session, q) if q else []
        # one query for all listed volunteers' team memberships (drives redaction + workload)
        team_sets = await team_ids_map(session, [v.id for v in found])
        list_defs = [
            d for d in await custom_field_service.list_defs(session) if d.show_in_list
        ]
        config = await workload_service.get_config(session)
        wl = await workload_service.visible_scores(session, actor, team_sets)

    shows_workload = actor.is_admin
    if band:
        # filtering happens strictly within the permitted set — no workload leak
        found = [v for v in found if v.id in wl and wl[v.id][1].label == band]

    panel = VolunteerPanel()
    with frame("Volunteers", actor):
        with ui.row().classes("items-center gap-2 w-full"):
            band_select: ui.select | None = None
            search = (
                ui.input("Search volunteers…", value=q)
                .props("outlined dense clearable")
                .classes("w-72")
            )

            def go() -> None:
                target = f"/volunteers?q={quote_plus(search.value or '')}"
                if band_select is not None and band_select.value:
                    target += f"&band={band_select.value}"
                ui.navigate.to(target)

            search.on("keydown.enter", go)
            ui.button("Search", on_click=go).props("dense")
            if shows_workload:
                band_select = (
                    ui.select(
                        {b.label: b.label for b in config.bands},
                        label="Workload",
                        value=band or None,
                        clearable=True,
                    )
                    .props("outlined dense")
                    .classes("w-40")
                )
                band_select.on_value_change(go)
            ui.space()
            if actor.is_admin:
                ui.button(
                    "New volunteer", icon="person_add", on_click=_new_volunteer_dialog
                ).props("dense")

        if team_hits:
            ui.label("Matching teams").classes("text-lg font-medium")
            with ui.row().classes("gap-2 w-full flex-wrap"):
                for team, path in team_hits:
                    ui.button(
                        path,
                        on_click=lambda _, tid=team.id: ui.navigate.to(f"/teams/{tid}"),
                    ).props("outline dense")

        columns = [
            {
                "name": "name",
                "label": "Name",
                "field": "name",
                "align": "left",
                "sortable": True,
            },
            {"name": "email", "label": "Email", "field": "email", "align": "left"},
            {"name": "phone", "label": "Phone", "field": "phone", "align": "left"},
        ]
        if shows_workload:
            columns.append(
                {
                    "name": "workload",
                    "label": "Workload",
                    "field": "workload",
                    "align": "left",
                }
            )
        for d in list_defs:
            columns.append(
                {
                    "name": f"cf_{d.key}",
                    "label": d.label,
                    "field": f"cf_{d.key}",
                    "align": "left",
                }
            )
        columns.append({"name": "status", "label": "", "field": "status"})

        rows = []
        for v in found:
            visible = actor.can_view_volunteer(v.id, team_sets.get(v.id, set()))
            row = {
                "id": v.id,
                "name": v.full_name,
                "email": (v.email or "") if visible else "•••",
                "phone": (v.phone or "") if visible else "•••",
                "status": "" if v.is_active else "inactive",
            }
            if shows_workload:
                score_band = wl.get(v.id)
                row["workload"] = score_band[1].label if score_band else ""
                row["workload_color"] = score_band[1].color if score_band else ""
                row["workload_score"] = (
                    f"{float(score_band[0]):g}" if score_band else ""
                )
            for d in list_defs:
                value = (v.custom or {}).get(d.key)
                row[f"cf_{d.key}"] = (
                    format_custom(d, value, missing="") if visible else "•••"
                )
            rows.append(row)
        table = ui.table(
            columns=columns, rows=rows, row_key="id", pagination=20
        ).classes("w-full vdb-clickable-rows")
        if shows_workload:
            table.add_slot(
                "body-cell-workload",
                """
                <q-td key="workload" :props="props">
                    <q-badge v-if="props.row.workload"
                             :style="{backgroundColor: props.row.workload_color}">
                        {{ props.row.workload }} · {{ props.row.workload_score }}
                    </q-badge>
                </q-td>
                """,
            )
        table.on("rowClick", lambda e: panel.open(e.args[1]["id"]))
        ui.label(f"{len(rows)} volunteer{'s' if len(rows) != 1 else ''}").classes(
            "text-sm text-gray-500"
        )


def _new_volunteer_dialog() -> None:
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("New volunteer").classes("text-lg font-medium")
        first = ui.input("First name").props("outlined dense").classes("w-full")
        last = ui.input("Last name").props("outlined dense").classes("w-full")
        email = ui.input("Email").props("outlined dense").classes("w-full")
        phone = ui.input("Phone").props("outlined dense").classes("w-full")

        @notify_errors
        async def save() -> None:
            if not (first.value or "").strip() or not (last.value or "").strip():
                ui.notify("First and last name are required", color="warning")
                return
            async with action_session() as (session, actor):
                require(actor.is_admin, "only admins create volunteers")
                v = await volunteer_service.create(
                    session,
                    first.value,
                    last.value,
                    email.value or None,
                    phone.value or None,
                )
                new_id = v.id
            dialog.close()
            ui.navigate.to(f"/volunteers/{new_id}")

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=save)
    dialog.open()


@ui.page("/volunteers/{volunteer_id}")
async def volunteer_detail(volunteer_id: int):
    async with page_session() as (session, actor):
        volunteer = await volunteer_service.get(session, volunteer_id)
        if volunteer is None:
            with frame("Volunteer not found", actor):
                ui.label(f"No volunteer with id {volunteer_id}.")
            return
        team_ids = await volunteer_team_ids(session, volunteer_id)
        can_view = actor.can_view_volunteer(volunteer_id, team_ids)
        can_edit = actor.can_edit_volunteer(volunteer_id, team_ids)
        field_defs = await custom_field_service.list_defs(session)
        wl = await workload_service.visible_scores(
            session, actor, {volunteer_id: team_ids}
        )
        assignments = await volunteer_service.assignments(session, volunteer_id)
        impact = (
            await volunteer_service.impact(session, volunteer_id) if can_view else []
        )
        spells = await volunteer_service.timeline(session, volunteer_id)
        all_teams = await team_service.list_all(session)
        paths = team_service.team_paths(all_teams)
        assignable = {
            t.id: paths[t.id] for t in all_teams if actor.can_manage_team(t.id)
        }
        photo_at = (await photo_service.versions(session, [volunteer_id])).get(
            volunteer_id
        )

    async def _reload() -> None:
        ui.navigate.reload()

    with frame(volunteer.full_name, actor):
        with ui.card().classes("w-full gap-1 p-4"):
            with ui.row().classes("items-center gap-2"):
                photo_avatar(
                    volunteer_id, volunteer.full_name, photo_at, on_change=_reload
                )
                ui.label(volunteer.full_name).classes("text-lg font-medium")
                if not volunteer.is_active:
                    ui.badge("inactive", color="grey")
                if volunteer_id in wl:
                    score, band = wl[volunteer_id]
                    ui.badge(f"workload: {band.label} · {float(score):g}").style(
                        f"background-color: {band.color}"
                    ).tooltip(
                        "Workload score: team weights × role multipliers, all ministries"
                    )
                ui.space()
                if can_edit:
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda: _edit_dialog(
                            volunteer, actor.is_admin, field_defs
                        ),
                    ).props("dense outline")
                if actor.is_admin:
                    ui.button(
                        "Delete",
                        icon="delete",
                        on_click=lambda: _delete_volunteer(volunteer_id),
                    ).props("dense outline color=negative")
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

        ui.label("Serves on").classes("text-lg font-medium")
        if not assignments:
            ui.label("Not on any team.").classes("text-gray-500")
        for membership, team in assignments:
            with ui.row().classes(
                "w-full items-center gap-2 p-2 rounded hover:bg-gray-100"
            ):
                ui.link(paths.get(team.id, team.name), f"/teams/{team.id}").classes(
                    "font-medium"
                )
                ui.badge(ROLE_LABELS[membership.role])
                ui.space()
                if membership.joined_on:
                    ui.label(f"since {membership.joined_on.isoformat()}").classes(
                        "text-xs text-gray-400"
                    )
                if actor.can_manage_team(team.id):
                    ui.button(
                        icon="person_remove",
                        on_click=notify_errors(
                            lambda _, mid=membership.id: _unassign(mid)
                        ),
                    ).props("dense flat color=negative").tooltip("Remove from team")

        ui.label("Service timeline").classes("text-lg font-medium")
        timeline_chart(spells, paths, dark=app.storage.user.get("dark_mode", False))

        if can_view:
            ui.label("If they leave, what holes appear?").classes("text-lg font-medium")
            if not impact:
                ui.label("No memberships — no holes.").classes("text-gray-500")
            for row in impact:
                critical = row.leadership_left == 0
                warn = row.leaders_left == 0 and not critical
                color = (
                    "bg-red-50"
                    if critical
                    else ("bg-amber-50" if warn else "bg-gray-50")
                )
                with ui.row().classes(f"w-full items-center gap-2 p-2 rounded {color}"):
                    ui.label(paths.get(row.team.id, row.team.name)).classes(
                        "font-medium"
                    )
                    ui.badge(ROLE_LABELS[row.role])
                    ui.space()
                    if critical:
                        ui.badge("team left with NO leadership", color="negative")
                    elif warn:
                        ui.badge("no leader left (second remains)", color="warning")
                    else:
                        ui.label(
                            f"{row.leaders_left} leader(s), {row.leadership_left} leadership total remain"
                        ).classes("text-sm text-gray-600")

        if assignable:
            ui.label("Add to team").classes("text-lg font-medium")
            with ui.row().classes("items-center gap-2"):
                team_select = (
                    ui.select(assignable, label="Team", with_input=True)
                    .props("outlined dense")
                    .classes("w-64")
                )
                role_select = (
                    ui.select(ROLE_OPTIONS, label="Role", value=TeamRole.member.value)
                    .props("outlined dense")
                    .classes("w-52")
                )

                @notify_errors
                async def add() -> None:
                    if not team_select.value:
                        ui.notify("Pick a team", color="warning")
                        return
                    async with action_session() as (session, actor):
                        require(
                            actor.can_manage_team(team_select.value), "manage this team"
                        )
                        await membership_service.assign(
                            session,
                            volunteer_id,
                            team_select.value,
                            TeamRole(role_select.value),
                        )
                    ui.navigate.reload()

                ui.button("Add", icon="group_add", on_click=add).props("dense")


def _custom_widget(defn: CustomFieldDef, value):
    """One editing widget per admin-defined field, matched to its type."""
    match FieldType(defn.field_type):
        case FieldType.number:
            return (
                ui.number(defn.label, value=value)
                .props("outlined dense clearable")
                .classes("w-full")
            )
        case FieldType.select:
            options = list(defn.options or [])
            return (
                ui.select(
                    options,
                    label=defn.label,
                    value=value if value in options else None,
                    clearable=True,
                )
                .props("outlined dense")
                .classes("w-full")
            )
        case FieldType.date:
            return (
                ui.input(defn.label, value=value or "", placeholder="YYYY-MM-DD")
                .props("outlined dense clearable")
                .classes("w-full")
            )
        case FieldType.checkbox:
            return ui.switch(defn.label, value=bool(value))
        case _:  # text
            return (
                ui.input(defn.label, value=value or "")
                .props("outlined dense")
                .classes("w-full")
            )


def _edit_dialog(
    volunteer, is_admin: bool, field_defs: list[CustomFieldDef] = ()
) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label(f"Edit {volunteer.full_name}").classes("text-lg font-medium")
        first = (
            ui.input("First name", value=volunteer.first_name)
            .props("outlined dense")
            .classes("w-full")
        )
        last = (
            ui.input("Last name", value=volunteer.last_name)
            .props("outlined dense")
            .classes("w-full")
        )
        email = (
            ui.input("Email", value=volunteer.email or "")
            .props("outlined dense")
            .classes("w-full")
        )
        phone = (
            ui.input("Phone", value=volunteer.phone or "")
            .props("outlined dense")
            .classes("w-full")
        )
        notes = (
            ui.textarea("Notes", value=volunteer.notes or "")
            .props("outlined dense")
            .classes("w-full")
        )
        custom_widgets = {
            defn.key: _custom_widget(defn, (volunteer.custom or {}).get(defn.key))
            for defn in field_defs
        }
        active = ui.switch("Active", value=volunteer.is_active) if is_admin else None

        @notify_errors
        async def save() -> None:
            values = {}
            for key, widget in custom_widgets.items():
                raw = widget.value
                if isinstance(raw, str):
                    raw = raw.strip() or None  # blank clears the field
                values[key] = raw
            async with action_session() as (session, actor):
                ids = await volunteer_team_ids(session, volunteer.id)
                require(
                    actor.can_edit_volunteer(volunteer.id, ids), "edit this volunteer"
                )
                await volunteer_service.update(
                    session,
                    volunteer.id,
                    first_name=first.value,
                    last_name=last.value,
                    email=email.value or None,
                    phone=phone.value or None,
                    notes=notes.value or None,
                    is_active=active.value if active is not None else None,
                )
                if values:
                    await custom_field_service.set_values(session, volunteer.id, values)
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


async def _unassign(membership_id: int) -> None:
    async with action_session() as (session, actor):
        membership = await membership_service.get(session, membership_id)
        if membership is None:
            raise LookupError("membership vanished")
        require(actor.can_manage_team(membership.team_id), "manage this team's roster")
        await membership_service.remove(session, membership_id)
    ui.navigate.reload()


@notify_errors
async def _delete_volunteer(volunteer_id: int) -> None:
    with ui.dialog() as dialog, ui.card().classes("gap-3"):
        ui.label("Delete this volunteer and all their memberships?").classes(
            "font-medium"
        )
        ui.label("History is preserved and visible in as-of views.").classes(
            "text-sm text-gray-500"
        )

        async def confirm() -> None:
            async with action_session() as (session, actor):
                require(actor.is_admin, "only admins delete volunteers")
                await volunteer_service.delete(session, volunteer_id)
            dialog.close()
            ui.navigate.to("/volunteers")

        with ui.row().classes("justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", on_click=notify_errors(confirm)).props("color=negative")
    dialog.open()
