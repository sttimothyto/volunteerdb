from datetime import datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from nicegui import app, ui

from .. import query_lang
from ..domain import EmailChangeAttempted
from ..env import current as current_env
from ..fp import Err
from ..models import ROLE_LABELS, CustomFieldDef, FieldType, TeamRole
from ..permissions import Actor, team_ids_map
from ..services import custom_fields as custom_field_service
from ..services import elections as elections_service
from ..services import memberships as membership_service
from ..services import readmodels
from ..services import teams as team_service
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from ..services.readmodels import VolunteerProfile
from ..services.volunteers import AddressChange
from . import column_order, invites
from .account_status import invitable, last_login_text
from .context import PageCtx, page_ctx, perform, rate_limit, run_command, toast
from .date_input import date_input, time_input
from .forms import actions, confirm, dialog_card
from .layout import frame
from .photo_dialog import photo_avatar
from .search_box import search_box
from .timeline_chart import timeline_chart
from .volunteer_panel import VolunteerPanel, format_custom
from .widgets import (
    ROLE_OPTIONS,
    inactive_badge,
    phase_badge,
    role_badge,
    workload_badge,
)


@ui.page("/volunteers")
async def volunteers_page(q: str = "", band: str = ""):
    is_query = query_lang.parse(q) is not None
    query_error: str | None = None
    async with page_ctx() as ctx:
        session, actor = ctx.session, ctx.actor
        result = await volunteer_service.search_or_query(
            session, q, include_inactive=actor.is_admin, actor=actor
        )
        if isinstance(result, Err):
            found, query_error = [], result.error.message
        else:
            found = result.value
        team_hits = await team_service.search(session, q) if q and not is_query else []
        # one query for all listed volunteers' team memberships (drives redaction + workload)
        team_sets = await team_ids_map(session, [v.id for v in found])
        list_defs = [
            d for d in await custom_field_service.list_defs(session) if d.show_in_list
        ]
        config = await workload_service.read_config(session)
        wl = await workload_service.visible_scores(session, actor, team_sets)

    shows_workload = actor.is_admin or bool(actor.managed_team_ids)
    if band:
        # filtering happens strictly within the permitted set — no workload leak
        found = [v for v in found if v.id in wl and wl[v.id][1].label == band]

    panel = VolunteerPanel("", ctx.base_url)
    with frame("Volunteers", actor):
        if query_error:
            ui.notify(query_error, color="warning")
        with ui.row().classes("items-center gap-2 w-full"):
            band_select: ui.select | None = None

            def go(text: str) -> None:
                target = f"/volunteers?q={quote_plus(text)}"
                if band_select is not None and band_select.value:
                    target += f"&band={band_select.value}"
                ui.navigate.to(target)

            search = search_box(
                "Search volunteers…",
                on_submit=go,
                on_pick_volunteer=panel.open,
                value=q,
            )
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
                band_select.on_value_change(lambda: go(search.value or ""))
            # no ui.space(): the search box grows instead, which is what keeps
            # New volunteer on the right edge
            if actor.is_admin:
                ui.button(
                    "New volunteer", icon="person_add", on_click=_new_volunteer_dialog
                ).props("dense")

        if team_hits:
            ui.label("Matching teams").classes("text-lg font-medium")
            with ui.row().classes("gap-2 w-full flex-wrap"):
                for team, path in team_hits:
                    ui.button(path).props(f'outline dense href="/teams/{team.id}"')

        columns = [
            {
                "name": "name",
                "label": "Name",
                "field": "name",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "email",
                "label": "Email",
                "field": "email",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "phone",
                "label": "Phone",
                "field": "phone",
                "align": "left",
                "sortable": True,
            },
        ]
        if shows_workload:
            columns.append(
                {
                    "name": "workload",
                    "label": "Workload",
                    # sorts on the score, not the band label: alphabetical bands
                    # would read Heavy < Light < Medium. The cell is drawn by the
                    # body-cell-workload slot below, so the field only sorts.
                    "field": "workload_sort",
                    "align": "left",
                    "sortable": True,
                }
            )
        for d in list_defs:
            columns.append(
                {
                    "name": f"cf_{d.key}",
                    "label": d.label,
                    "field": f"cf_{d.key}",
                    "align": "left",
                    "sortable": True,
                }
            )
        columns.append(
            {"name": "status", "label": "Status", "field": "status", "sortable": True}
        )

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
                row["workload_text"] = (
                    workload_service.text_colour(score_band[1].color)
                    if score_band
                    else ""
                )
                row["workload_score"] = (
                    f"{float(score_band[0]):g}" if score_band else ""
                )
                # scores are sums of non-negative weights, so -1 parks the
                # unscored rows below every real score when the column sorts
                row["workload_sort"] = float(score_band[0]) if score_band else -1.0
            for d in list_defs:
                value = (v.custom or {}).get(d.key)
                row[f"cf_{d.key}"] = (
                    format_custom(d, value, missing="") if visible else "•••"
                )
            rows.append(row)
        columns = column_order.apply_saved_order("volunteers", columns)
        table = ui.table(
            columns=columns, rows=rows, row_key="id", pagination=20
        ).classes("w-full vdb-clickable-rows")
        column_order.make_draggable(table, "volunteers")
        # a real button in the name cell, so the row opens from the keyboard
        # too; its click bubbles to the row, which is what opens the panel
        table.add_slot(
            "body-cell-name",
            '<q-td key="name" :props="props"><button type="button" '
            'class="vdb-rowbtn">{{ props.row.name }}</button></q-td>',
        )
        if shows_workload:
            table.add_slot(
                "body-cell-workload",
                """
                <q-td key="workload" :props="props">
                    <q-badge v-if="props.row.workload"
                             :style="{backgroundColor: props.row.workload_color, color: props.row.workload_text}">
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
    with dialog_card("New volunteer") as dialog:
        first = ui.input("First name").props("outlined dense").classes("w-full")
        last = ui.input("Last name").props("outlined dense").classes("w-full")
        email = ui.input("Email").props("outlined dense").classes("w-full")
        phone = ui.input("Phone").props("outlined dense").classes("w-full")

        async def save() -> None:
            if not (first.value or "").strip() or not (last.value or "").strip():
                ui.notify("First and last name are required", color="warning")
                return

            async def command(ctx: PageCtx):
                return await volunteer_service.create(
                    ctx.session,
                    ctx.actor,
                    first.value,
                    last.value,
                    email.value or None,
                    phone.value or None,
                )

            def done(volunteer, _effects, _report) -> None:
                dialog.close()
                ui.navigate.to(f"/volunteers/{volunteer.id}")

            await run_command(command, on_ok=done, reload=False)

        actions(dialog, "Create", save)
    dialog.open()


# --- the profile's sections ----------------------------------------------------
#
# One function per block on /volunteers/{id}, in the order the page draws
# them; each takes the profile (readmodels.volunteer_profile) or the rows it
# draws. The handlers they drive are module-level and take ids.


async def _reload_page() -> None:
    ui.navigate.reload()


def _contact_details(profile: VolunteerProfile) -> None:
    volunteer = profile.volunteer
    ui.label(f"Email: {volunteer.email or '—'}").classes("text-sm text-gray-700")
    ui.label(f"Phone: {volunteer.phone or '—'}").classes("text-sm text-gray-700")
    for defn in profile.field_defs:
        value = (volunteer.custom or {}).get(defn.key)
        ui.label(f"{defn.label}: {format_custom(defn, value)}").classes(
            "text-sm text-gray-700"
        )
    if profile.can_edit and volunteer.notes:
        ui.label(f"Notes: {volunteer.notes}").classes("text-sm text-gray-700")
    hours = profile.hours
    if hours is not None and hours.events_attended:
        ui.label(
            f"Service hours: {hours.total_hours:g} h across "
            f"{hours.events_attended} event"
            f"{'s' if hours.events_attended != 1 else ''}"
        ).classes("text-sm text-gray-700").tooltip(
            "Derived from event attendance: scheduled duration "
            "unless a leader recorded an exception"
        )


def _profile_card(profile: VolunteerProfile, actor: Actor, base_url: str) -> None:
    """Name, photo and badges, with Edit and Delete for those who may; the
    contact details for those who may read them; and for everyone the
    sign-in status -- whether someone reads what the app sends them is not
    a contact detail."""
    volunteer = profile.volunteer
    with ui.card().classes("w-full gap-1 p-4"):
        with ui.row().classes("items-center gap-2"):
            photo_avatar(
                volunteer.id,
                volunteer.full_name,
                profile.photo_at,
                on_change=_reload_page,
            )
            ui.label(volunteer.full_name).classes("text-lg font-medium")
            if not volunteer.is_active:
                inactive_badge()
            if profile.workload is not None:
                workload_badge(*profile.workload, prefix="workload: ")
            ui.space()
            if profile.can_edit:
                ui.button(
                    "Edit",
                    icon="edit",
                    on_click=lambda: _edit_dialog(
                        volunteer, actor, profile.field_defs, base_url=base_url
                    ),
                ).props("dense outline")
            if actor.is_admin:
                ui.button(
                    "Delete",
                    icon="delete",
                    on_click=lambda: _delete_volunteer(volunteer.id),
                ).props("dense outline color=negative")
        if profile.can_view:
            _contact_details(profile)
        else:
            ui.label(
                "Contact details visible to their team leaders and core members."
            ).classes("text-sm text-gray-400 italic")
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.label(f"Last login: {last_login_text(profile.account)}").classes(
                "text-sm text-gray-700"
            )
            # just the control here: the line above already says the status
            if (
                actor.can_invite_volunteer(profile.team_ids)
                and volunteer.is_active
                and invitable(profile.account)
            ):
                invites.invite_control(
                    volunteer.id,
                    volunteer.full_name,
                    volunteer.email,
                    profile.account,
                    base_url,
                    reveal=actor.is_admin,
                    where="profile",
                )


def _serves_on_section(profile: VolunteerProfile, actor: Actor) -> None:
    ui.label("Serves on").classes("text-lg font-medium")
    if not profile.assignments:
        ui.label("Not on any team.").classes("text-gray-500")
    for membership, team in profile.assignments:
        with ui.row().classes(
            "w-full items-center gap-2 p-2 rounded hover:bg-gray-100"
        ):
            ui.link(profile.paths.get(team.id, team.name), f"/teams/{team.id}").classes(
                "font-medium"
            )
            role_badge(membership.role)
            ui.space()
            if actor.can_manage_team(team.id):
                ui.button(
                    icon="person_remove",
                    on_click=lambda _, mid=membership.id: _unassign(mid),
                ).props("dense flat color=negative").tooltip("Remove from team")


def _add_to_team_row(volunteer_id: int, assignable: dict[int, str]) -> None:
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
        ui.button(
            "Add",
            icon="group_add",
            on_click=lambda: _add_to_team(
                volunteer_id, team_select.value, role_select.value
            ),
        ).props("dense")


def _timeline_section(
    profile: VolunteerProfile, *, now: datetime, tz: ZoneInfo
) -> None:
    ui.label("Service timeline").classes("text-lg font-medium")
    timeline_chart(
        profile.spells,
        profile.paths,
        dark=app.storage.user.get("dark_mode", False),
        now=now,
        tz=tz,
    )


def _impact_section(profile: VolunteerProfile) -> None:
    """The priest's question: if they leave, what holes appear?"""
    ui.label("If they leave, what vacancies appear?").classes("text-lg font-medium")
    if not profile.impact:
        ui.label("No memberships — no holes.").classes("text-gray-500")
    for row in profile.impact:
        critical = row.leadership_left == 0
        warn = row.leaders_left == 0 and not critical
        color = "bg-red-50" if critical else ("bg-amber-50" if warn else "bg-gray-50")
        with ui.row().classes(f"w-full items-center gap-2 p-2 rounded {color}"):
            ui.label(profile.paths.get(row.team.id, row.team.name)).classes(
                "font-medium"
            )
            role_badge(row.role)
            ui.space()
            if critical:
                ui.badge("team left with NO leadership", color="negative")
            elif warn:
                ui.badge("no leader left (second remains)", color="warning")
            else:
                ui.label(
                    f"{row.leaders_left} leader(s), {row.leadership_left} leadership total remain"
                ).classes("text-sm text-gray-600")


def _involvements_section(
    involvements: list[elections_service.ProposalInvolvement],
) -> None:
    ui.label("Proposals involving them").classes("text-lg font-medium")
    for inv in involvements:
        proposal = inv.proposal
        with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-gray-50"):
            ui.link(
                f"{inv.path}: {ROLE_LABELS[TeamRole(proposal.role)]}",
                f"/elections/{proposal.id}",
            ).classes("font-medium")
            if inv.appointed:
                # the person-badge implies the proposal state, so the
                # phase badge (which would repeat "Appointed") is skipped
                ui.badge("Appointed", color="positive")
            else:
                phase_badge(proposal, inv.phase)
                if inv.as_candidate:
                    ui.badge("Candidate", color="primary").props("outline")
            if inv.as_voter:
                ui.badge("Voting member").props("outline")


@ui.page("/volunteers/{volunteer_id}")
async def volunteer_detail(volunteer_id: int):
    async with page_ctx() as ctx:
        actor, tz = ctx.actor, ctx.env.tz
        shown = await readmodels.volunteer_profile(
            ctx.session, actor, volunteer_id, now=ctx.now, tz=tz
        )
    if isinstance(shown, Err):
        with frame("Volunteer not found", actor):
            ui.label(f"No volunteer with id {volunteer_id}.")
        return
    profile = shown.value

    with frame(profile.volunteer.full_name, actor):
        _profile_card(profile, actor, ctx.base_url)
        _serves_on_section(profile, actor)
        if profile.assignable:
            _add_to_team_row(volunteer_id, profile.assignable)
        _timeline_section(profile, now=ctx.now, tz=tz)
        if profile.can_view:
            _impact_section(profile)
        if profile.involvements:
            _involvements_section(profile.involvements)


async def _add_to_team(volunteer_id: int, team_id: int | None, role_value: str) -> None:
    if not team_id:
        ui.notify("Pick a team", color="warning")
        return

    async def command(ctx: PageCtx):
        return await membership_service.assign(
            ctx.session, ctx.actor, volunteer_id, team_id, TeamRole(role_value)
        )

    await run_command(command, reload=True)


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
            return date_input(defn.label, value=value or "", clearable=True).classes(
                "w-full"
            )
        case FieldType.time:
            return time_input(defn.label, value=value or "", clearable=True).classes(
                "w-full"
            )
        case FieldType.checkbox:
            return ui.switch(defn.label, value=bool(value))
        case FieldType.integer:
            return (
                ui.number(defn.label, value=value, precision=0)
                .props("outlined dense clearable")
                .classes("w-full")
            )
        case (
            FieldType.decimal
            | FieldType.timestamp
            | FieldType.timestamptz
            | FieldType.interval
            | FieldType.uuid
        ) as ft:
            # typed as text, like date: the codec validates on save
            placeholders = {
                FieldType.decimal: "e.g. 12.50",
                FieldType.timestamp: "YYYY-MM-DD HH:MM",
                FieldType.timestamptz: "YYYY-MM-DD HH:MM+02:00",
                FieldType.interval: "P1DT2H30M (ISO 8601 duration)",
                FieldType.uuid: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            }
            return (
                ui.input(defn.label, value=value or "", placeholder=placeholders[ft])
                .props("outlined dense clearable")
                .classes("w-full")
            )
        case _:  # text
            return (
                ui.input(defn.label, value=value or "")
                .props("outlined dense")
                .classes("w-full")
            )


def _edit_dialog(
    volunteer,
    actor: Actor,
    field_defs: list[CustomFieldDef] = (),
    *,
    base_url: str = "",
) -> None:
    """The contact-detail editor. One field behaves differently for one
    person: your own address is not written here but staged and mailed a
    confirmation link, because it is also what you sign in with. The rule
    that decides when is the service's (volunteers.address_change); this
    dialog only acts on its answer. Everything else saves immediately."""
    is_self = actor.volunteer_id == volunteer.id
    with dialog_card(f"Edit {volunteer.full_name}", width="w-[34rem]") as dialog:
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
            .mark("edit-email")
        )
        if is_self:
            ui.label(
                "Changing your own address sends a confirmation link to the "
                "new one; nothing moves until you open it."
            ).classes("text-xs text-gray-500")
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
        active = (
            ui.switch("Active", value=volunteer.is_active) if actor.is_admin else None
        )

        async def save() -> None:
            values = {}
            for key, widget in custom_widgets.items():
                raw = widget.value
                if isinstance(raw, str):
                    raw = raw.strip() or None  # blank clears the field
                values[key] = raw
            change = volunteer_service.address_change(actor, volunteer, email.value)
            if change is AddressChange.blank_own:
                ui.notify(
                    "Your own address cannot be blank — it is how you sign in.",
                    color="warning",
                )
                return
            staged = None
            if change is AddressChange.needs_confirmation:
                staged = (email.value or "").strip().lower()
                # charge the send budget the /account and API doors charge, on
                # every attempt (before the service reveals whether the address
                # is taken), so this door is not the loose one
                now = current_env().clock.now()
                if denied := rate_limit(
                    f"email-change:{actor.user.id}",
                    now=now,
                    what="change your email address",
                ):
                    toast(denied.error)
                    return
                await perform(
                    [EmailChangeAttempted(actor.user.id)], base_url=base_url, now=now
                )
            fields = {} if staged else {"email": email.value or None}

            # somebody else's address moving is worth a word to the address it
            # moved away from: the service says so (AddressReplaced) and the
            # policy mails it after the commit
            async def command(ctx: PageCtx):
                updated = await volunteer_service.update(
                    ctx.session,
                    ctx.actor,
                    volunteer.id,
                    first_name=first.value,
                    last_name=last.value,
                    phone=phone.value or None,
                    notes=notes.value or None,
                    is_active=active.value if active is not None else None,
                    **fields,
                )
                if isinstance(updated, Err):
                    return updated
                if values:
                    put = await custom_field_service.set_values(
                        ctx.session, ctx.actor, volunteer.id, values
                    )
                    if isinstance(put, Err):
                        return put
                return updated

            saved = await run_command(command, reload=False)
            if isinstance(saved, Err):
                return
            if staged:
                await _stage_own_email(staged)
            dialog.close()
            ui.navigate.reload()

        actions(dialog, "Save", save)
    dialog.open()


async def _stage_own_email(address: str) -> None:
    """Arm the confirmation link for the signed-in account; the policy mails
    the address being claimed and warns the one being replaced, after the
    commit. Same flow as the /account page."""

    async def command(ctx: PageCtx):
        return await user_service.start_email_change(
            ctx.session,
            ctx.actor.user.id,
            address,
            now=ctx.now,
            token=ctx.env.rng.token(),
        )

    def done(value, _effects, _report) -> None:
        account, _token = value
        ui.notify(
            f"Confirmation sent to {account.pending_email}. Your address changes "
            "when you open the link in it.",
            color="positive",
            multi_line=True,
            timeout=8000,
        )

    await run_command(command, on_ok=done, reload=False)


async def _unassign(membership_id: int) -> None:
    await run_command(
        lambda ctx: membership_service.remove(ctx.session, ctx.actor, membership_id)
    )


async def _delete_volunteer(volunteer_id: int) -> None:
    if not await confirm(
        "Delete this volunteer and all their memberships?",
        detail="History is preserved and visible in as-of views.",
        yes="Delete",
        danger=True,
    ):
        return

    async def command(ctx: PageCtx):
        return await volunteer_service.delete(ctx.session, ctx.actor, volunteer_id)

    await run_command(
        command, on_ok=lambda _v, _e, _r: ui.navigate.to("/volunteers"), reload=False
    )
