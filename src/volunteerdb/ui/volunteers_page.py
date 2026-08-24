from urllib.parse import quote_plus

from fastapi import Request
from nicegui import app, ui

from .. import query_lang, throttle
from ..log import audit_log
from ..models import ROLE_LABELS, CustomFieldDef, FieldType, TeamRole
from ..permissions import team_ids_map, volunteer_team_ids
from ..services import custom_fields as custom_field_service
from ..services import elections as elections_service
from ..services import events as event_service
from ..services import mail
from ..services import memberships as membership_service
from ..services import photos as photo_service
from ..services import teams as team_service
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from . import column_order, invites
from .account_status import invitable, last_login_text
from .context import action_session, notify_errors, page_session
from .elections_page import phase_badge
from .layout import frame
from .login import confirm_email_url
from .photo_dialog import photo_avatar
from .search_box import search_box
from .timeline_chart import timeline_chart
from .volunteer_panel import VolunteerPanel, format_custom

ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}


@ui.page("/volunteers")
async def volunteers_page(request: Request, q: str = "", band: str = ""):
    is_query = query_lang.parse(q) is not None
    query_error: str | None = None
    async with page_session() as (session, actor):
        try:
            found = await volunteer_service.search_or_query(
                session, q, include_inactive=actor.is_admin, actor=actor
            )
        except query_lang.QueryError as exc:
            found, query_error = [], str(exc)
        team_hits = await team_service.search(session, q) if q and not is_query else []
        # one query for all listed volunteers' team memberships (drives redaction + workload)
        team_sets = await team_ids_map(session, [v.id for v in found])
        list_defs = [
            d for d in await custom_field_service.list_defs(session) if d.show_in_list
        ]
        config = await workload_service.get_config(session)
        wl = await workload_service.visible_scores(session, actor, team_sets)

    shows_workload = actor.is_admin or bool(actor.managed_team_ids)
    if band:
        # filtering happens strictly within the permitted set — no workload leak
        found = [v for v in found if v.id in wl and wl[v.id][1].label == band]

    panel = VolunteerPanel("", str(request.base_url).rstrip("/"))
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
                v = await volunteer_service.create(
                    session,
                    actor,
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
async def volunteer_detail(request: Request, volunteer_id: int):
    base_url = str(request.base_url).rstrip("/")
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
            await volunteer_service.impact(session, actor, volunteer_id)
            if can_view
            else []
        )
        # scoped inside the service: only proposals this actor may see
        involvements = await elections_service.involving(session, actor, volunteer_id)
        hours = (
            await event_service.hours_for_volunteer(session, actor, volunteer_id)
            if can_view
            else None
        )
        spells = await volunteer_service.timeline(session, volunteer_id)
        account = await user_service.account_for_volunteer(session, volunteer_id)
        tree = await team_service.tree(session)
        paths = tree.paths
        assignable = {
            t.id: paths[t.id] for t in tree.teams if actor.can_manage_team(t.id)
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
                    ui.badge("inactive", color="muted")
                if volunteer_id in wl:
                    score, band = wl[volunteer_id]
                    ui.badge(f"workload: {band.label} · {float(score):g}").style(
                        f"background-color: {band.color}; "
                        f"color: {workload_service.text_colour(band.color)}"
                    ).tooltip(
                        "Workload score: team weights × role multipliers, all ministries"
                    )
                ui.space()
                if can_edit:
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda: _edit_dialog(
                            volunteer,
                            actor.is_admin,
                            field_defs,
                            is_self=volunteer_id == actor.volunteer_id,
                            base_url=base_url,
                            own_login=actor.user.email,
                            own_user_id=actor.user.id,
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
                if hours is not None and hours.events_attended:
                    ui.label(
                        f"Service hours: {hours.total_hours:g} h across "
                        f"{hours.events_attended} event"
                        f"{'s' if hours.events_attended != 1 else ''}"
                    ).classes("text-sm text-gray-700").tooltip(
                        "Derived from event attendance: scheduled duration "
                        "unless a leader recorded an exception"
                    )
            else:
                ui.label(
                    "Contact details visible to their team leaders and core members."
                ).classes("text-sm text-gray-400 italic")
            # outside the can_view gate on purpose: whether someone reads what
            # the app sends them is not a contact detail
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.label(f"Last login: {last_login_text(account)}").classes(
                    "text-sm text-gray-700"
                )
                # just the control here: the line above already says the status
                if (
                    actor.can_invite_volunteer(team_ids)
                    and volunteer.is_active
                    and invitable(account)
                ):
                    invites.invite_control(
                        volunteer_id,
                        volunteer.full_name,
                        volunteer.email,
                        account,
                        base_url,
                        reveal=actor.is_admin,
                        where="profile",
                    )

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
                if actor.can_manage_team(team.id):
                    ui.button(
                        icon="person_remove",
                        on_click=notify_errors(
                            lambda _, mid=membership.id: _unassign(mid)
                        ),
                    ).props("dense flat color=negative").tooltip("Remove from team")

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
                        await membership_service.assign(
                            session,
                            actor,
                            volunteer_id,
                            team_select.value,
                            TeamRole(role_select.value),
                        )
                    ui.navigate.reload()

                ui.button("Add", icon="group_add", on_click=add).props("dense")

        ui.label("Service timeline").classes("text-lg font-medium")
        timeline_chart(spells, paths, dark=app.storage.user.get("dark_mode", False))

        if can_view:
            ui.label("If they leave, what vacancies appear?").classes(
                "text-lg font-medium"
            )
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

        if involvements:
            ui.label("Proposals involving them").classes("text-lg font-medium")
            for inv in involvements:
                proposal = inv.proposal
                with ui.row().classes(
                    "w-full items-center gap-2 p-2 rounded bg-gray-50"
                ):
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
            | FieldType.time
            | FieldType.interval
            | FieldType.uuid
        ) as ft:
            # typed as text, like date: the codec validates on save
            placeholders = {
                FieldType.decimal: "e.g. 12.50",
                FieldType.timestamp: "YYYY-MM-DD HH:MM",
                FieldType.timestamptz: "YYYY-MM-DD HH:MM+02:00",
                FieldType.time: "HH:MM",
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
    is_admin: bool,
    field_defs: list[CustomFieldDef] = (),
    *,
    is_self: bool = False,
    base_url: str = "",
    own_login: str | None = None,
    own_user_id: int | None = None,
) -> None:
    """The contact-detail editor. `is_self` changes exactly one field's
    behaviour: your own address is not written here but staged and mailed a
    confirmation link, because it is also what you sign in with. Everything
    else in the form saves immediately either way.

    The one exception: typing the address you ALREADY sign in with just syncs
    the volunteer row onto it — no confirmation, because there is nothing to
    confirm — which is the only way to fill a linked record whose email is
    blank (own_login / own_user_id name that signed-in account)."""
    with ui.dialog() as dialog, ui.card().classes("w-[34rem] gap-3"):
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
        active = ui.switch("Active", value=volunteer.is_active) if is_admin else None

        @notify_errors
        async def save() -> None:
            values = {}
            for key, widget in custom_widgets.items():
                raw = widget.value
                if isinstance(raw, str):
                    raw = raw.strip() or None  # blank clears the field
                values[key] = raw
            typed = (email.value or "").strip().lower()
            on_file = (volunteer.email or "").strip().lower()
            login = (own_login or "").strip().lower()
            # your own address goes the long way round — UNLESS you are only
            # syncing your record onto the address you already sign in with,
            # which is already confirmed and so needs no round-trip (and is the
            # one way to fill a linked record whose email is blank). Everyone
            # else's is a plain edit, as it has to be — a leader correcting a
            # bounced address cannot wait on the person who cannot read their mail.
            staged = typed if is_self and typed != on_file and typed != login else None
            if is_self and not typed:
                ui.notify(
                    "Your own address cannot be blank — it is how you sign in.",
                    color="warning",
                )
                return
            if staged and own_user_id is not None:
                # F1: charge the send budget the /account and API doors charge,
                # on every attempt (before the service reveals whether the
                # address is taken), so this door is not the loose one.
                key = f"email-change:{own_user_id}"
                if throttle.blocked(key, 5, 900):
                    ui.notify(
                        "Too many address changes requested — try again in a "
                        "few minutes.",
                        color="negative",
                    )
                    return
                throttle.hit(key)
            fields = {} if staged else {"email": email.value or None}
            # somebody else's address moving is worth a word to the address it
            # moved away from; see _notify_replaced_address
            replaced = on_file if not is_self and on_file and typed != on_file else None
            async with action_session() as (session, actor):
                await volunteer_service.update(
                    session,
                    actor,
                    volunteer.id,
                    first_name=first.value,
                    last_name=last.value,
                    phone=phone.value or None,
                    notes=notes.value or None,
                    is_active=active.value if active is not None else None,
                    **fields,
                )
                if values:
                    await custom_field_service.set_values(
                        session, actor, volunteer.id, values
                    )
            if staged:
                await _stage_own_email(staged, base_url)
            elif replaced:
                await _notify_replaced_address(volunteer.id, replaced, typed, base_url)
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


async def _notify_replaced_address(
    volunteer_id: int, was: str, now: str, base_url: str
) -> None:
    """Tell the address a leader just moved a volunteer away from.

    The edit itself is immediate and stays that way — a leader correcting a
    bounced address cannot wait on somebody who cannot read their mail. But a
    redirected address is also the first step of a takeover (point it at your
    own, ask for their invite, redeem it), so the mailbox losing the account
    hears about it on a channel the person doing the edit does not control.
    After the commit, like every other message this page sends."""
    audit_log(
        "volunteer.address_replaced_by_other",
        volunteer_id=volunteer_id,
        was=was,
        now=now or "(none)",
    )
    if not now:  # cleared rather than redirected: nothing to point them at
        return
    await mail.send_email(was, *mail.address_edited_email(now, f"{base_url}/login"))


async def _stage_own_email(address: str, base_url: str) -> None:
    """Arm the confirmation link for the signed-in account, mail it to the
    address being claimed, and warn the one being replaced. Same flow as the
    /account page; both messages go out after the commit."""
    async with action_session() as (session, actor):
        account, token = await user_service.start_email_change(
            session, actor.user.id, address
        )
        target, user_id, was = account.pending_email, actor.user.id, actor.user.email
    audit_log("auth.email_change_requested", user=f"{user_id}:{was}", to=target)
    hours = int(user_service.EMAIL_CHANGE_TTL.total_seconds() // 3600)
    await mail.send_email(
        target,
        *mail.email_change_email(confirm_email_url(base_url, token), target, hours),
    )
    # and the address being replaced hears about it while it can still say no
    await mail.send_email(
        was, *mail.email_change_requested_email(target, f"{base_url}/account", hours)
    )
    ui.notify(
        f"Confirmation sent to {target}. Your address changes when you open "
        "the link in it.",
        color="positive",
        multi_line=True,
        timeout=8000,
    )


async def _unassign(membership_id: int) -> None:
    async with action_session() as (session, actor):
        await membership_service.remove(session, actor, membership_id)
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
                await volunteer_service.delete(session, actor, volunteer_id)
            dialog.close()
            ui.navigate.to("/volunteers")

        with ui.row().classes("justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", on_click=notify_errors(confirm)).props("color=negative")
    dialog.open()
