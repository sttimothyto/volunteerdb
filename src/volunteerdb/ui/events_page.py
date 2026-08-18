"""Events: the scheduling pages.

/events is everyone's home base — your upcoming duties (with the
substitution flow), open substitute requests you could claim, and the
upcoming events on your teams; managers also create events here (with the
weekly repeat helper). /events/{id} is one event's workroom: slots with
sign-up/assignment, per-event RSVPs, and — once the event has ended — the
attendance record with manager-recorded exceptions.

Every action handler re-checks permissions inside its own action_session,
and mail goes out only AFTER the transaction committed (send_email never
raises), with links derived from the live request.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from nicegui import ui
from starlette.requests import Request

from .. import query_lang
from ..config import settings
from ..log import audit_log
from ..models import EventSlot, EventStatus, EventSubRequest, Volunteer
from ..permissions import Forbidden, require
from ..services import events as event_service
from ..services import gcal, mail
from ..services import interest as interest_service
from ..services import task_force as task_force_service
from ..services import teams as team_service
from . import column_order
from .context import action_session, notify_errors, page_session
from .date_input import date_input
from .layout import frame


def _tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


def _status_badge(event) -> None:
    if event.status == EventStatus.cancelled.value:
        ui.badge("Cancelled", color="grey")
    elif event_service.is_past(event):
        ui.badge("Past", color="purple")
    else:
        ui.badge(f"{event.starts_at.astimezone(_tz()):%a %b %-d}", color="primary")


def _parse_local(day_s: str, time_s: str, what: str) -> datetime | None:
    try:
        return datetime.combine(
            date.fromisoformat(day_s or ""), time.fromisoformat(time_s or ""), _tz()
        )
    except ValueError:
        ui.notify(f"{what}: use YYYY-MM-DD and HH:MM", color="warning")
        return None


def _event_count(shown: int, total: int | None = None) -> str:
    if total is None or shown == total:
        return f"{shown} event{'s' if shown != 1 else ''}"
    return f"{shown} of {total} events"


def _wire_search(
    search: ui.input, count: ui.label, table: ui.table, rows: list[dict]
) -> None:
    """Narrow the table as you type — the teams-page idiom: every listed event
    is already in `rows`, so the filter swaps what the table shows without a
    query or a reload."""

    def matches(row: dict, text: str) -> bool:
        return any(
            text in (row[key] or "").lower()
            for key in ("title", "team", "location", "when")
        )

    def apply() -> None:
        text = (search.value or "").strip()
        ast = query_lang.parse(text) if text else None
        if ast is None:
            shown = rows if not text else [r for r in rows if matches(r, text.lower())]
        else:
            try:
                pred = query_lang.compile_events(ast)
            except query_lang.QueryError as exc:
                # inline, not a toast: this filter runs on every keystroke
                count.set_text(f"query error: {exc}")
                return
            shown = [r for r in rows if pred(r)]
        table.rows = shown
        table.update()
        count.set_text(_event_count(len(shown), len(rows)))

    search.on_value_change(apply)


def _share_event(base_url: str, event_id: int) -> None:
    """Copy the event link, then say what the recipients will need.

    The copy happens up front (the click IS the intent); the dialog exists for
    the caveat — the link asks for a sign-in, so a leader texting it out has to
    make sure everyone on the list has an account first.
    """
    url = f"{base_url}/events/{event_id}"
    ui.clipboard.write(url)
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Event link copied").classes("text-lg font-medium")
        ui.input(value=url).props("readonly outlined dense").classes("w-full")
        ui.label(
            "Before you email or text this link out, make sure every "
            "volunteer has a VolunteerDB account — opening it asks for a "
            "sign-in, and the event is shown only to members of its team."
        ).classes("text-sm text-gray-500")
        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Close", on_click=dialog.close).props("flat dense")
    dialog.open()


async def _sub_request_dialog(assignment_id: int, base_url: str) -> None:
    """Open a substitution call and mail the teammates who could take it."""
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Ask for a substitute").classes("text-lg font-medium")
        ui.label(
            "Your teammates get one email; the first to claim the slot "
            "takes it. You stay on the hook until someone does."
        ).classes("text-sm text-gray-500")
        note = (
            ui.input("Note to the team (optional)")
            .props("outlined dense")
            .classes("w-full")
        )

        @notify_errors
        async def save() -> None:
            async with action_session() as (session, actor):
                assignment = await event_service.get_assignment(session, assignment_id)
                if assignment is None:
                    raise LookupError("assignment vanished")
                sub = await event_service.request_sub(
                    session,
                    actor,
                    assignment_id=assignment_id,
                    requested_by=actor.user.id,
                    note=note.value,
                )
                view = await event_service.detail(session, actor, assignment.event_id)
                asker = next(
                    v
                    for sv in view.slots
                    for a, v in sv.entries
                    if a.id == assignment_id
                )
                slot_name = next(
                    sv.slot.name
                    for sv in view.slots
                    if sv.slot.id == assignment.slot_id
                )
                audience = await event_service.member_emails(
                    session,
                    view.event.team_id,
                    exclude_volunteer_ids=await event_service.assigned_volunteer_ids(
                        session, view.event.id
                    ),
                )
                message = mail.sub_request_email(
                    view.event.title,
                    view.path,
                    slot_name,
                    mail.event_when(view.event.starts_at, view.event.ends_at),
                    asker.full_name,
                    sub.note,
                    f"{base_url}/events",
                )
            for address in audience:  # after commit; send_email never raises
                await mail.send_email(address, *message)
            dialog.close()
            ui.notify(
                f"Asked {len(audience)} teammate(s) for a substitute",
                color="positive",
            )
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Ask the team", icon="campaign", on_click=save)
    dialog.open()


async def _substitute_dialog(
    assignment_id: int, options: dict[int, str], base_url: str
) -> None:
    """Hand a slot straight to a chosen teammate — no open call, no race."""
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Hand this slot to a teammate").classes("text-lg font-medium")
        ui.label(
            "They take the slot immediately and are emailed about it. The "
            "change is recorded: who made it, and when, goes into the log."
        ).classes("text-sm text-gray-500")
        pick = (
            ui.select(options, label="Who takes it?", with_input=True)
            .props("outlined dense")
            .classes("w-full")
        )

        @notify_errors
        async def save() -> None:
            if not pick.value:
                ui.notify("Pick a teammate first", color="warning")
                return
            async with action_session() as (session, actor):
                assignment = await event_service.get_assignment(session, assignment_id)
                if assignment is None:
                    raise LookupError("assignment vanished")
                event = await event_service.get(session, assignment.event_id)
                if event is None:
                    raise LookupError("event vanished")
                assignment, outgoing, incoming = await event_service.substitute(
                    session,
                    actor,
                    assignment_id=assignment_id,
                    new_volunteer_id=pick.value,
                    acted_by=actor.user.id,
                )
                slot = await session.get(EventSlot, assignment.slot_id)
                message = mail.substituted_in_email(
                    event.title,
                    slot.name if slot else "volunteer",
                    mail.event_when(event.starts_at, event.ends_at),
                    outgoing.full_name,
                    f"{base_url}/events",
                )
                incoming_email = incoming.email
                incoming_name = incoming.full_name
                audit_log(
                    "event.substitute",
                    event_id=event.id,
                    assignment_id=assignment_id,
                    from_volunteer_id=outgoing.id,
                    to_volunteer_id=incoming.id,
                )
            if incoming_email:  # after commit; send_email never raises
                await mail.send_email(incoming_email, *message)
            dialog.close()
            ui.notify(f"{incoming_name} now holds the slot", color="positive")
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Hand it over", icon="swap_horiz", on_click=save)
    dialog.open()


async def _self_removal_dialog(assignment_id: int) -> None:
    """Take yourself off a slot, telling the leaders why."""
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Take yourself off this slot").classes("text-lg font-medium")
        ui.label(
            "Say why — your reason is emailed to the team leader(s) so they "
            "can fill the gap."
        ).classes("text-sm text-gray-500")
        reason = (
            ui.textarea("Why can you no longer serve?")
            .props("outlined dense rows=3")
            .classes("w-full")
        )

        @notify_errors
        async def save() -> None:
            text = (reason.value or "").strip()
            if not text:
                ui.notify("A reason is required", color="warning")
                return
            async with action_session() as (session, actor):
                assignment = await event_service.get_assignment(session, assignment_id)
                if assignment is None:
                    raise LookupError("assignment vanished")
                # remove_assignment allows a manager too; taking YOURSELF off
                # is the flow this dialog serves, and the reason it collects
                require(
                    assignment.volunteer_id == actor.volunteer_id,
                    "take somebody else off their slot",
                )
                event = await event_service.get(session, assignment.event_id)
                if event is None:
                    raise LookupError("event vanished")
                slot = await session.get(EventSlot, assignment.slot_id)
                me = await session.get(Volunteer, assignment.volunteer_id)
                paths = team_service.team_paths(await team_service.list_all(session))
                audience = await interest_service.leader_emails(session, event.team_id)
                message = mail.self_removal_email(
                    event.title,
                    paths.get(event.team_id, ""),
                    slot.name if slot else "volunteer",
                    mail.event_when(event.starts_at, event.ends_at),
                    me.full_name if me else "A volunteer",
                    text,
                )
                await event_service.remove_assignment(session, actor, assignment_id)
                audit_log(
                    "event.self_removal",
                    event_id=event.id,
                    volunteer_id=actor.volunteer_id,
                    reason=text[:500],
                )
            for address in audience:  # after commit; send_email never raises
                await mail.send_email(address, *message)
            dialog.close()
            ui.notify(
                "You're off the slot — the leaders have been told",
                color="positive",
            )
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Take me off", on_click=save).props("color=negative")
    dialog.open()


@notify_errors
async def _claim_sub(sub_request_id: int) -> None:
    async with action_session() as (session, actor):
        sub, assignment, asker = await event_service.claim_sub(
            session,
            actor,
            sub_request_id=sub_request_id,
            volunteer_id=actor.volunteer_id,
        )
        event = await event_service.get(session, assignment.event_id)
        slot = await session.get(EventSlot, assignment.slot_id)
        claimant = await session.get(Volunteer, actor.volunteer_id)
        message = mail.sub_claimed_email(
            event.title,
            slot.name,
            mail.event_when(event.starts_at, event.ends_at),
            claimant.full_name if claimant else "A teammate",
            asker.full_name,
        )
        recipients = set(await interest_service.leader_emails(session, event.team_id))
        if asker.email:
            recipients.add(asker.email)
    for address in sorted(recipients):  # after commit
        await mail.send_email(address, *message)
    ui.notify("The slot is yours — thank you!", color="positive")
    ui.navigate.reload()


@notify_errors
async def _withdraw_sub(sub_request_id: int) -> None:
    async with action_session() as (session, actor):
        sub = await session.get(EventSubRequest, sub_request_id)
        if sub is None:
            raise LookupError("request vanished")
        await event_service.cancel_sub(session, actor, sub_request_id)
    ui.notify("Request withdrawn", color="positive")
    ui.navigate.reload()


async def _confirm_similar(hits: list[event_service.SimilarEvent]) -> bool:
    """The double-booking warning: advisory, never a block. A masked title
    means the colliding event belongs to a team outside the creator's view —
    the when/where is the warning; the details stay theirs."""
    with ui.dialog() as dialog, ui.card().classes("w-[28rem] gap-3"):
        ui.label("Possible double booking").classes("text-lg font-medium")
        ui.label(
            "Something similar is already on the calendar at that location "
            "on the same day:"
        ).classes("text-sm text-gray-500")
        for hit in hits:
            with ui.column().classes("w-full gap-0 p-2 rounded bg-amber-50"):
                ui.label(hit.title or "Another team's event").classes("font-medium")
                ui.label(
                    f"{mail.event_when(hit.starts_at, hit.ends_at)} · "
                    f"{hit.location} · {hit.team_path}"
                ).classes("text-sm text-gray-600")
        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Go back", on_click=lambda: dialog.submit(False)).props("flat")
            ui.button("Create anyway", on_click=lambda: dialog.submit(True)).props(
                "color=warning"
            )
    return bool(await dialog)


def _new_event_dialog(managed_options: dict[int, str]) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-[30rem] gap-3"):
        ui.label("New event").classes("text-lg font-medium")
        team = (
            ui.select(managed_options, label="Team", with_input=True)
            .props("outlined dense")
            .classes("w-full")
        )
        title = ui.input("Title").props("outlined dense").classes("w-full")
        tomorrow = date.today() + timedelta(days=1)
        with ui.row().classes("w-full gap-2"):
            day = date_input("Date (YYYY-MM-DD)", value=str(tomorrow)).classes("grow")
            start = (
                ui.input("Starts (HH:MM)", value="10:00")
                .props("outlined dense")
                .classes("w-28")
            )
            end = (
                ui.input("Ends (HH:MM)", value="12:00")
                .props("outlined dense")
                .classes("w-28")
            )
        location = (
            ui.input("Location (optional)").props("outlined dense").classes("w-full")
        )
        description = (
            ui.textarea("Description (optional)")
            .props("outlined dense rows=2")
            .classes("w-full")
        )
        ui.label("Slots — leave capacity blank for unlimited").classes(
            "text-sm text-gray-500"
        )
        slot_rows: list[tuple[ui.input, ui.number]] = []
        slots_col = ui.column().classes("w-full gap-1")

        def add_slot_row(name: str = "", capacity: int | None = None) -> None:
            with slots_col, ui.row().classes("w-full gap-2 items-center"):
                n = ui.input("Slot", value=name).props("outlined dense").classes("grow")
                c = (
                    ui.number("Capacity", value=capacity, min=1, precision=0)
                    .props("outlined dense clearable")
                    .classes("w-32")
                )
            slot_rows.append((n, c))

        add_slot_row("Volunteers")
        ui.button(
            "Add another slot", icon="add", on_click=lambda: add_slot_row()
        ).props("flat dense no-caps")
        repeat = date_input(
            "Repeat weekly until (YYYY-MM-DD, optional)", clearable=True
        ).classes("w-full")

        @notify_errors
        async def save() -> None:
            if not team.value:
                ui.notify("Pick the team", color="warning")
                return
            starts_at = _parse_local(day.value, start.value, "Start")
            ends_at = _parse_local(day.value, end.value, "End")
            if starts_at is None or ends_at is None:
                return
            until: date | None = None
            if repeat.value:
                try:
                    until = date.fromisoformat(repeat.value)
                except ValueError:
                    ui.notify("Repeat until: use YYYY-MM-DD", color="warning")
                    return
            slots = [
                event_service.SlotInput(
                    n.value.strip(), int(c.value) if c.value else None, i
                )
                for i, (n, c) in enumerate(slot_rows)
                if (n.value or "").strip()
            ]
            async with action_session() as (session, actor):
                hits = await event_service.similar_events(
                    session,
                    actor,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    repeat_until=until,
                    location=location.value,
                )
            if hits and not await _confirm_similar(hits):
                return  # back to the still-open form
            async with action_session() as (session, actor):
                created = await event_service.create_event(
                    session,
                    actor,
                    team_id=team.value,
                    title=title.value or "",
                    starts_at=starts_at,
                    ends_at=ends_at,
                    description=description.value,
                    location=location.value,
                    slots=slots,
                    repeat_weekly_until=until,
                    created_by=actor.user.id,
                )
                first_id = created[0].id
                n_created = len(created)
            dialog.close()
            if n_created > 1:
                ui.notify(f"{n_created} events created", color="positive")
            ui.navigate.to(f"/events/{first_id}")

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create event", icon="event", on_click=save)
    dialog.open()


@ui.page("/events")
async def events_page(request: Request, past: str = ""):
    base_url = str(request.base_url).rstrip("/")
    show_past = past == "1"
    async with page_session() as (session, actor):
        duties = (
            await event_service.my_upcoming(session, actor.volunteer_id)
            if actor.volunteer_id is not None
            else []
        )
        claimable = await event_service.claimable_subs(session, actor)
        now = datetime.now(_tz())
        summaries = await event_service.list_events(
            session,
            actor,
            from_=None if show_past else now,
            to=now if show_past else None,
            include_cancelled=show_past,
        )
        managed_options: dict[int, str] = {}
        if actor.can_create_events:
            all_teams = await team_service.list_all(session)
            paths = team_service.team_paths(all_teams)
            managed_options = {
                t.id: paths[t.id]
                for t in all_teams
                if t.is_active and actor.can_manage_team(t.id)
            }
    if show_past:
        summaries = list(reversed(summaries))  # most recent past first

    with frame("Events", actor):
        if duties:
            ui.label("Your upcoming duties").classes("text-lg font-medium")
            with ui.column().classes("w-full gap-1"):
                for duty in duties:
                    with ui.row().classes(
                        "w-full items-center gap-2 p-2 rounded bg-gray-50"
                    ):
                        ui.link(duty.event.title, f"/events/{duty.event.id}").classes(
                            "font-medium"
                        )
                        ui.badge(duty.slot.name)
                        ui.label(
                            mail.event_when(duty.event.starts_at, duty.event.ends_at)
                        ).classes("text-sm text-gray-600")
                        ui.space()
                        if duty.open_sub is not None:
                            ui.badge("sub wanted", color="warning")
                            ui.button(
                                "Withdraw request",
                                on_click=lambda _, sid=duty.open_sub.id: _withdraw_sub(
                                    sid
                                ),
                            ).props("dense flat")
                        else:
                            ui.button(
                                "Need a sub",
                                icon="campaign",
                                on_click=lambda _, aid=duty.assignment.id: (
                                    _sub_request_dialog(aid, base_url)
                                ),
                            ).props("dense outline")

        if claimable:
            ui.label("Teammates need a substitute").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for c in claimable:
                    with ui.row().classes(
                        "w-full items-center gap-2 p-2 rounded bg-amber-50"
                    ):
                        ui.label(c.volunteer.full_name).classes("font-medium")
                        ui.label(f"needs a {c.slot.name} at").classes("text-sm")
                        ui.link(c.event.title, f"/events/{c.event.id}")
                        ui.label(
                            mail.event_when(c.event.starts_at, c.event.ends_at)
                        ).classes("text-sm text-gray-600")
                        if c.sub.note:
                            ui.label(f"“{c.sub.note}”").classes("text-sm text-gray-500")
                        ui.space()
                        ui.button(
                            "Take this slot",
                            icon="volunteer_activism",
                            on_click=lambda _, sid=c.sub.id: _claim_sub(sid),
                        ).props("dense outline")

        # the public parish calendar, when one is configured — Google serves
        # the embed itself, so this costs the page nothing but the iframe
        embed = gcal.embed_url()
        if embed and not show_past:
            ui.element("iframe").props(f'src="{embed}"').classes(
                "w-full rounded mt-4"
            ).style("height: 24rem; border: 0").mark("gcal-embed")

        rows = []
        for s in summaries:
            local = s.event.starts_at.astimezone(_tz())
            if s.my_assignment is not None:
                you = "serving"
            elif s.my_rsvp is not None:
                you = "available" if s.my_rsvp.available else "unavailable"
            else:
                you = ""
            rows.append(
                {
                    "id": s.event.id,
                    "when": f"{local:%Y-%m-%d %H:%M}",
                    "title": s.event.title
                    + (
                        " (cancelled)"
                        if s.event.status == EventStatus.cancelled.value
                        else ""
                    ),
                    "team": s.path,
                    "location": s.event.location or "",
                    "filled": f"{s.filled}/{s.capacity if s.capacity is not None else '∞'}",
                    # numeric twins for sorting and the query language
                    "filled_n": s.filled,
                    "capacity_n": s.capacity,
                    "you": you,
                }
            )
        with ui.row().classes("w-full items-center mt-4"):
            ui.label(
                ("Past events" if show_past else "Upcoming events")
                + (" (all teams)" if actor.is_admin else " on your teams")
            ).classes("text-lg font-medium")
            # the search box grows into the free space and holds the buttons
            # against the right edge (the teams-page idiom); with nothing to
            # search the spacer takes over that job
            search = (
                ui.input("Search events…")
                .props("outlined dense clearable debounce=200")
                .classes("grow")
                if rows
                else None
            )
            if search is None:
                ui.space()
            ui.button(
                "Show upcoming" if show_past else "Show past",
                on_click=lambda: ui.navigate.to(
                    "/events" if show_past else "/events?past=1"
                ),
            ).props("dense flat no-caps")
            if managed_options:
                ui.button(
                    "New event",
                    icon="event",
                    on_click=lambda: _new_event_dialog(managed_options),
                ).props("dense outline")

        if rows:
            columns = [
                {
                    "name": "when",
                    "label": "When",
                    "field": "when",
                    "align": "left",
                    "sortable": True,  # ISO strings: lexicographic = chronological
                },
                {
                    "name": "title",
                    "label": "Event",
                    "field": "title",
                    "align": "left",
                    "sortable": True,
                },
                {
                    "name": "team",
                    "label": "Team",
                    "field": "team",
                    "align": "left",
                    "sortable": True,
                },
                {
                    "name": "location",
                    "label": "Location",
                    "field": "location",
                    "align": "left",
                    "sortable": True,
                },
                # sorts on the count; the cell slot shows the pretty "3/∞"
                {
                    "name": "filled",
                    "label": "Filled",
                    "field": "filled_n",
                    "sortable": True,
                },
                {
                    "name": "you",
                    "label": "You",
                    "field": "you",
                    "align": "left",
                    "sortable": True,
                },
            ]
            columns = column_order.apply_saved_order("events", columns)
            table = ui.table(
                columns=columns,
                rows=rows,
                row_key="id",
                # upcoming is one screen like before; the past list grows forever
                pagination=20 if show_past else 0,
            ).classes("w-full vdb-clickable-rows")
            column_order.make_draggable(table, "events")
            table.add_slot(
                "body-cell-filled",
                '<q-td key="filled" :props="props">{{ props.row.filled }}</q-td>',
            )
            table.on("rowClick", lambda e: ui.navigate.to(f"/events/{e.args[1]['id']}"))
            count = ui.label(_event_count(len(rows))).classes("text-sm text-gray-500")
            if search is not None:
                _wire_search(search, count, table, rows)
        else:
            ui.label(
                "Nothing scheduled yet."
                + (
                    ""
                    if not managed_options
                    else " Create the first event with the button above."
                )
            ).classes("text-gray-500")


def _edit_event_dialog(event) -> None:
    local_start = event.starts_at.astimezone(_tz())
    local_end = event.ends_at.astimezone(_tz())
    with ui.dialog() as dialog, ui.card().classes("w-[28rem] gap-3"):
        ui.label("Edit event").classes("text-lg font-medium")
        title = (
            ui.input("Title", value=event.title)
            .props("outlined dense")
            .classes("w-full")
        )
        with ui.row().classes("w-full gap-2"):
            day = date_input(
                "Date (YYYY-MM-DD)", value=str(local_start.date())
            ).classes("grow")
            start = (
                ui.input("Starts (HH:MM)", value=f"{local_start:%H:%M}")
                .props("outlined dense")
                .classes("w-28")
            )
            end = (
                ui.input("Ends (HH:MM)", value=f"{local_end:%H:%M}")
                .props("outlined dense")
                .classes("w-28")
            )
        location = (
            ui.input("Location", value=event.location or "")
            .props("outlined dense")
            .classes("w-full")
        )
        description = (
            ui.textarea("Description", value=event.description or "")
            .props("outlined dense rows=2")
            .classes("w-full")
        )

        @notify_errors
        async def save() -> None:
            starts_at = _parse_local(day.value, start.value, "Start")
            ends_at = _parse_local(day.value, end.value, "End")
            if starts_at is None or ends_at is None:
                return
            async with action_session() as (session, actor):
                current = await event_service.get(session, event.id)
                if current is None:
                    raise LookupError("event vanished")
                await event_service.update_event(
                    session,
                    actor,
                    event.id,
                    title=title.value or "",
                    description=description.value,
                    location=location.value,
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


def _add_slot_dialog(event_id: int) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Add a slot").classes("text-lg font-medium")
        name = ui.input("Slot name").props("outlined dense").classes("w-full")
        capacity = (
            ui.number("Capacity (blank = unlimited)", min=1, precision=0)
            .props("outlined dense clearable")
            .classes("w-full")
        )

        @notify_errors
        async def save() -> None:
            async with action_session() as (session, actor):
                current = await event_service.get(session, event_id)
                if current is None:
                    raise LookupError("event vanished")
                await event_service.add_slot(
                    session,
                    actor,
                    event_id,
                    name=name.value or "",
                    capacity=int(capacity.value) if capacity.value else None,
                )
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Add slot", on_click=save)
    dialog.open()


@ui.page("/events/{event_id}")
async def event_detail_page(request: Request, event_id: int):
    base_url = str(request.base_url).rstrip("/")
    async with page_session() as (session, actor):
        try:
            view = await event_service.detail(session, actor, event_id)
        except LookupError:
            with frame("Event not found", actor):
                ui.label(f"No event with id {event_id}.")
            return
        except Forbidden:
            # the service decides; the page only chooses how to say it, and a
            # whole page reads better than a toast on an empty frame
            with frame("Events", actor):
                ui.label("This event is visible to the members of its team.").classes(
                    "text-gray-500"
                )
            return
        event = view.event
        can_manage = actor.can_manage_team(event.team_id)
        am_member = actor.volunteer_id is not None and await event_service.is_member(
            session, actor.volunteer_id, event.team_id
        )
        am_assigned = actor.volunteer_id is not None and any(
            v.id == actor.volunteer_id for sv in view.slots for _, v in sv.entries
        )
        # members holding a slot need the roster too: the hand-off picker
        # shows names, which everyone past can_view_roster_names may see
        roster = (
            await team_service.roster(session, actor, event.team_id)
            if can_manage or am_assigned
            else []
        )
        attendance = (
            await event_service.attendance_rows(session, actor, event_id)
            if can_manage
            and event_service.is_past(event)
            and event.status == EventStatus.scheduled.value
            else None
        )
        tf_view = await task_force_service.get_for_event(session, event_id)
        collaborator_options: dict[int, str] = {}
        if can_manage:
            all_teams = await team_service.list_all(session)
            paths = team_service.team_paths(all_teams)
            staffing = {t.id for t in tf_view.sources} if tf_view else {event.team_id}
            if tf_view:
                staffing.add(tf_view.task_force.team_id)
            collaborator_options = {
                t.id: paths[t.id]
                for t in all_teams
                if t.is_active and t.id not in staffing
            }
            source_paths = (
                [paths.get(t.id, t.name) for t in tf_view.sources] if tf_view else []
            )

    upcoming = (
        event.status == EventStatus.scheduled.value and not event_service.is_past(event)
    )
    sub_wanted = {a.id: sub for sub, a in view.open_subs}
    rsvp_by_vid = {v.id: r for r, v in view.rsvps}
    my_rsvp = rsvp_by_vid.get(actor.volunteer_id) if actor.volunteer_id else None
    my_assignment = next(
        (a for sv in view.slots for a, v in sv.entries if v.id == actor.volunteer_id),
        None,
    )
    assigned_vids = {v.id for sv in view.slots for _, v in sv.entries}

    def picker_options(exclude: set[int]) -> dict[int, str]:
        """Team roster minus the excluded, available RSVPs first."""

        def rank(vid: int) -> int:
            rsvp = rsvp_by_vid.get(vid)
            if rsvp is None:
                return 1
            return 0 if rsvp.available else 2

        entries = sorted(
            ((m, v) for m, v in roster if v.id not in exclude),
            key=lambda mv: (rank(mv[1].id), mv[1].last_name, mv[1].first_name),
        )
        suffix = {0: " · available", 1: "", 2: " · UNAVAILABLE"}
        return {v.id: f"{v.full_name}{suffix[rank(v.id)]}" for _, v in entries}

    @notify_errors
    async def _rsvp(available: bool, note_value: str) -> None:
        async with action_session() as (session, actor):
            await event_service.set_rsvp(
                session,
                actor,
                event_id=event_id,
                volunteer_id=actor.volunteer_id,
                available=available,
                note=note_value,
            )
        ui.navigate.reload()

    def _signup_dialog(slot_id: int, slot_name: str) -> None:
        series = event.series_id is not None
        with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
            ui.label(f"Sign up — {slot_name}").classes("text-lg font-medium")
            repeat = None
            if series:
                repeat = ui.checkbox(
                    "Also sign me up for the later weeks of this series"
                ).props("dense")
                ui.label(
                    "Weeks already full, or where you already serve, are skipped."
                ).classes("text-sm text-gray-500")
            ui.label("Email me a reminder:").classes("text-sm text-gray-500")
            week = ui.checkbox("7 days before", value=True).props("dense")
            day = ui.checkbox("24 hours before", value=True).props("dense")

            @notify_errors
            async def save() -> None:
                async with action_session() as (session, actor):
                    if repeat is not None and repeat.value:
                        _, result = await event_service.sign_up_series(
                            session,
                            actor,
                            slot_id=slot_id,
                            volunteer_id=actor.volunteer_id,
                            notify_7d=bool(week.value),
                            notify_24h=bool(day.value),
                        )
                    else:
                        await event_service.sign_up(
                            session,
                            actor,
                            slot_id=slot_id,
                            volunteer_id=actor.volunteer_id,
                            notify_7d=bool(week.value),
                            notify_24h=bool(day.value),
                        )
                        result = None
                dialog.close()
                if result is None or result == event_service.SeriesSignupResult(
                    0, 0, 0
                ):
                    ui.notify("You're on the list", color="positive")
                else:
                    skipped = result.skipped_full + result.skipped_conflict
                    ui.notify(
                        f"You're on the list — this week plus {result.joined} more"
                        + (f", {skipped} week(s) skipped" if skipped else ""),
                        color="positive",
                    )
                ui.navigate.reload()

            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Sign up", icon="person_add", on_click=save).mark(
                    "signup-confirm"
                )
        dialog.open()

    async def _confirm_add_collaborator(team_label: str) -> bool:
        with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
            ui.label(f"Add {team_label} to this event?").classes("font-medium")
            ui.label(
                "A temporary task-force team is created holding both rosters: "
                "members of the added team can sign up for slots, its leaders "
                "co-manage the event, and the team is removed automatically "
                "after the event ends (it stays visible in history)."
            ).classes("text-sm text-gray-500")
            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button(
                    "Add team", icon="group_add", on_click=lambda: dialog.submit(True)
                ).mark("confirm-collaborator")
        return bool(await dialog)

    @notify_errors
    async def _add_collaborator(team_id_value) -> None:
        if not team_id_value:
            ui.notify("Pick a team first", color="warning")
            return
        label = collaborator_options.get(team_id_value, "that team")
        if not await _confirm_add_collaborator(label):
            return
        async with action_session() as (session, actor):
            meta = await task_force_service.add_collaborating_team(
                session,
                actor,
                event_id=event_id,
                source_team_id=team_id_value,
                created_by=actor.user.id,
            )
            audit_log(
                "event.collaboration_added",
                event_id=event_id,
                source_team_id=team_id_value,
                task_force_team_id=meta.id,
            )
        ui.notify("Team added — their roster can sign up now", color="positive")
        ui.navigate.reload()

    @notify_errors
    async def _sync_rosters() -> None:
        async with action_session() as (session, actor):
            added = await task_force_service.refresh_rosters(session, actor, event_id)
        ui.notify(f"Rosters synced — {added} member(s) added", color="positive")
        ui.navigate.reload()

    @notify_errors
    async def _withdraw(assignment_id: int) -> None:
        async with action_session() as (session, actor):
            await event_service.remove_assignment(session, actor, assignment_id)
        ui.navigate.reload()

    @notify_errors
    async def _assign(slot_id: int, volunteer_id: int | None) -> None:
        if not volunteer_id:
            ui.notify("Pick a person first", color="warning")
            return
        async with action_session() as (session, actor):
            current = await event_service.get(session, event_id)
            if current is None:
                raise LookupError("event vanished")
            await event_service.assign(
                session,
                actor,
                slot_id=slot_id,
                volunteer_id=volunteer_id,
                assigned_by=actor.user.id,
            )
        ui.navigate.reload()

    @notify_errors
    async def _delete_slot(slot_id: int) -> None:
        async with action_session() as (session, actor):
            current = await event_service.get(session, event_id)
            if current is None:
                raise LookupError("event vanished")
            await event_service.delete_slot(session, actor, slot_id)
        ui.navigate.reload()

    async def _cancel_event() -> None:
        with ui.dialog() as confirm, ui.card().classes("w-96 gap-3"):
            ui.label(
                "Cancel this event? Everyone signed up is emailed, and open "
                "substitute requests are closed with it."
            )
            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Keep it", on_click=lambda: confirm.submit(False)).props(
                    "flat"
                )
                ui.button(
                    "Yes, cancel it", on_click=lambda: confirm.submit(True)
                ).props("color=negative")
        if not await confirm:
            return
        await _do_cancel()

    @notify_errors
    async def _do_cancel() -> None:
        async with action_session() as (session, actor):
            current = await event_service.get(session, event_id)
            if current is None:
                raise LookupError("event vanished")
            was_upcoming = not event_service.is_past(current)
            cancelled, emails = await event_service.cancel_event(
                session, actor, event_id, cancelled_by=actor.user.id
            )
            paths = team_service.team_paths(await team_service.list_all(session))
            message = mail.event_cancelled_email(
                cancelled.title,
                paths.get(cancelled.team_id, ""),
                mail.event_when(cancelled.starts_at, cancelled.ends_at),
            )
        if was_upcoming:  # after commit; nobody needs mail about a past event
            for address in emails:
                await mail.send_email(address, *message)
        ui.navigate.reload()

    @notify_errors
    async def _save_attendance(
        assignment_id: int, attended_value: bool, hours_value
    ) -> None:
        try:
            hours = Decimal(str(hours_value)) if hours_value is not None else None
        except InvalidOperation:
            ui.notify("Hours must be a number", color="warning")
            return
        async with action_session() as (session, actor):
            current = await event_service.get(session, event_id)
            if current is None:
                raise LookupError("event vanished")
            await event_service.set_attendance(
                session,
                actor,
                assignment_id=assignment_id,
                attended=attended_value,
                hours=hours,
            )
        ui.notify("Attendance saved", color="positive")
        ui.navigate.reload()

    @notify_errors
    async def _clear_attendance(assignment_id: int) -> None:
        async with action_session() as (session, actor):
            current = await event_service.get(session, event_id)
            if current is None:
                raise LookupError("event vanished")
            await event_service.set_attendance(
                session, actor, assignment_id=assignment_id, attended=None, hours=None
            )
        ui.navigate.reload()

    with frame(event.title, actor):
        with ui.row().classes("w-full items-center gap-2"):
            ui.link(view.path, f"/teams/{event.team_id}").classes("font-medium")
            _status_badge(event)
            ui.label(mail.event_when(event.starts_at, event.ends_at)).classes(
                "text-sm text-gray-600"
            )
            if event.location:
                ui.label(f"· {event.location}").classes("text-sm text-gray-600")
            ui.space()
            ui.button(
                "Share",
                icon="share",
                on_click=lambda: _share_event(base_url, event_id),
            ).props("dense outline").mark("share-event")
            if can_manage and event.status == EventStatus.scheduled.value:
                ui.button(
                    "Edit", icon="edit", on_click=lambda: _edit_event_dialog(event)
                ).props("dense outline")
                ui.button("Cancel event", on_click=_cancel_event).props(
                    "dense outline color=negative"
                )
        if event.description:
            ui.label(event.description).classes("text-sm text-gray-600")

        if can_manage and upcoming:
            with ui.card().classes("w-full gap-2 p-3"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label("Collaboration").classes("font-medium")
                    if tf_view is not None:
                        ui.badge("task force", color="secondary")
                    ui.space()
                    if tf_view is not None:
                        ui.button(
                            "Sync rosters", icon="sync", on_click=_sync_rosters
                        ).props("dense flat").tooltip(
                            "Re-copy the source rosters — people who joined a "
                            "staffing team since then join the task force"
                        )
                if tf_view is not None:
                    ui.label("Staffed by: " + " · ".join(source_paths)).classes(
                        "text-sm text-gray-600"
                    )
                else:
                    ui.label(
                        "Need another ministry for this event? Adding a "
                        "collaborating team creates a temporary task-force "
                        "team holding both rosters, so everyone can sign up; "
                        "it is removed automatically after the event."
                    ).classes("text-sm text-gray-500")
                if collaborator_options:
                    with ui.row().classes("w-full items-center gap-2"):
                        collab_pick = (
                            ui.select(
                                collaborator_options,
                                label="Add collaborating team",
                                with_input=True,
                            )
                            .props("outlined dense")
                            .classes("w-72")
                        )
                        # default-arg capture: the slots section below rebinds
                        # its own `pick`, and a bare closure would read that
                        ui.button(
                            "Add",
                            icon="group_add",
                            on_click=lambda p=collab_pick: _add_collaborator(p.value),
                        ).props("dense outline").mark("add-collaborator")

        if am_member and upcoming:
            with ui.card().classes("w-full gap-2 p-3"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label("Can you serve at this event?").classes("font-medium")
                    if my_rsvp is not None:
                        ui.badge(
                            "you said: available"
                            if my_rsvp.available
                            else "you said: not available",
                            color="positive" if my_rsvp.available else "grey",
                        )
                    ui.space()
                    note = (
                        ui.input(
                            "Note (optional)",
                            value=my_rsvp.note if my_rsvp else "",
                        )
                        .props("outlined dense")
                        .classes("w-64")
                    )
                    ui.button(
                        "Available",
                        icon="thumb_up",
                        on_click=lambda: _rsvp(True, note.value),
                    ).props("dense outline color=positive")
                    ui.button(
                        "Not available",
                        icon="thumb_down",
                        on_click=lambda: _rsvp(False, note.value),
                    ).props("dense outline")

        ui.label("Slots").classes("text-lg font-medium mt-2")
        for sv in view.slots:
            slot = sv.slot
            with ui.card().classes("w-full gap-2 p-3"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(slot.name).classes("font-medium")
                    filled = len(sv.entries)
                    cap = "∞" if slot.capacity is None else str(slot.capacity)
                    ui.badge(f"{filled}/{cap}")
                    ui.space()
                    if (
                        am_member
                        and upcoming
                        and my_assignment is None
                        and (sv.open_spots is None or sv.open_spots > 0)
                    ):
                        ui.button(
                            "Sign up",
                            icon="person_add",
                            on_click=lambda _, sid=slot.id, sn=slot.name: (
                                _signup_dialog(sid, sn)
                            ),
                        ).props("dense outline")
                    if can_manage and upcoming and not sv.entries:
                        ui.button(
                            icon="delete",
                            on_click=lambda _, sid=slot.id: _delete_slot(sid),
                        ).props("dense flat").tooltip("Remove this empty slot")
                for assignment, volunteer in sv.entries:
                    with ui.row().classes(
                        "w-full items-center gap-2 p-1 rounded hover:bg-gray-100"
                    ):
                        ui.label(volunteer.full_name)
                        if assignment.kind == "sub":
                            ui.badge("substitute", color="secondary")
                        rsvp = rsvp_by_vid.get(volunteer.id)
                        if rsvp is not None and not rsvp.available:
                            ui.badge("marked unavailable", color="warning")
                        if assignment.id in sub_wanted:
                            ui.badge("sub wanted", color="warning")
                        ui.space()
                        if (
                            upcoming
                            and volunteer.id == actor.volunteer_id
                            and assignment.id not in sub_wanted
                        ):
                            ui.button(
                                "Need a sub",
                                icon="campaign",
                                on_click=lambda _, aid=assignment.id: (
                                    _sub_request_dialog(aid, base_url)
                                ),
                            ).props("dense outline")
                        if upcoming and volunteer.id == actor.volunteer_id:
                            # handing off with an open sub call cancels the call
                            ui.button(
                                "Hand off",
                                icon="swap_horiz",
                                on_click=lambda _, aid=assignment.id: (
                                    _substitute_dialog(
                                        aid, picker_options(assigned_vids), base_url
                                    )
                                ),
                            ).props("dense outline")
                            ui.button(
                                "Withdraw",
                                on_click=lambda _, aid=assignment.id: (
                                    _self_removal_dialog(aid)
                                ),
                            ).props("dense flat")
                        elif upcoming and can_manage:
                            ui.button(
                                "Remove",
                                on_click=lambda _, aid=assignment.id: _withdraw(aid),
                            ).props("dense flat")
                if can_manage and upcoming:
                    options = picker_options(assigned_vids)
                    if options and (sv.open_spots is None or sv.open_spots > 0):
                        with ui.row().classes("w-full items-center gap-2"):
                            pick = (
                                ui.select(
                                    options,
                                    label="Schedule someone",
                                    with_input=True,
                                )
                                .props("outlined dense")
                                .classes("w-64")
                            )
                            ui.button(
                                "Assign",
                                on_click=lambda _, sid=slot.id, p=pick: _assign(
                                    sid, p.value
                                ),
                            ).props("dense outline")
        if can_manage and upcoming:
            ui.button(
                "Add slot", icon="add", on_click=lambda: _add_slot_dialog(event_id)
            ).props("dense flat no-caps")

        if can_manage and view.rsvps:
            ui.label("Availability answers").classes("text-lg font-medium mt-2")
            with ui.column().classes("w-full gap-1"):
                for rsvp, volunteer in view.rsvps:
                    with ui.row().classes("w-full items-center gap-2 p-1"):
                        ui.label(volunteer.full_name)
                        ui.badge(
                            "available" if rsvp.available else "not available",
                            color="positive" if rsvp.available else "grey",
                        )
                        if rsvp.note:
                            ui.label(f"“{rsvp.note}”").classes("text-sm text-gray-500")

        eligible_subs = [
            (sub, a)
            for sub, a in view.open_subs
            if am_member
            and upcoming
            and a.volunteer_id != actor.volunteer_id
            and actor.volunteer_id not in assigned_vids
        ]
        if eligible_subs:
            ui.label("Substitutes wanted").classes("text-lg font-medium mt-2")
            names = {v.id: v.full_name for sv in view.slots for _, v in sv.entries}
            slot_names = {sv.slot.id: sv.slot.name for sv in view.slots}
            for sub, a in eligible_subs:
                with ui.row().classes(
                    "w-full items-center gap-2 p-2 rounded bg-amber-50"
                ):
                    ui.label(
                        f"{names.get(a.volunteer_id, 'A teammate')} needs a "
                        f"{slot_names.get(a.slot_id, 'substitute')}"
                    )
                    if sub.note:
                        ui.label(f"“{sub.note}”").classes("text-sm text-gray-500")
                    ui.space()
                    ui.button(
                        "Take this slot",
                        icon="volunteer_activism",
                        on_click=lambda _, sid=sub.id: _claim_sub(sid),
                    ).props("dense outline")

        if attendance is not None:
            ui.label("Attendance").classes("text-lg font-medium mt-2")
            ui.label(
                "Everyone assigned counts as attended for the scheduled "
                f"duration ({event_service.scheduled_hours(event)} h) unless "
                "corrected here."
            ).classes("text-sm text-gray-500")
            if not attendance:
                ui.label("Nobody was assigned to this event.").classes("text-gray-500")
            for assignment, slot, volunteer in attendance:
                attended, hours = event_service.effective(assignment, event)
                overridden = (
                    assignment.attended_override is not None
                    or assignment.hours_override is not None
                )
                with ui.row().classes("w-full items-center gap-3 p-1"):
                    ui.label(volunteer.full_name).classes("w-48")
                    ui.badge(slot.name)
                    box = ui.checkbox("attended", value=attended).props("dense")
                    hrs = (
                        ui.number("hours", value=float(hours), min=0, step=0.25)
                        .props("outlined dense")
                        .classes("w-28")
                    )
                    if overridden:
                        ui.badge("adjusted", color="secondary")
                    ui.space()
                    ui.button(
                        "Save",
                        on_click=lambda _, aid=assignment.id, b=box, h=hrs: (
                            _save_attendance(aid, b.value, h.value)
                        ),
                    ).props("dense flat")
                    if overridden:
                        ui.button(
                            "Reset",
                            on_click=lambda _, aid=assignment.id: _clear_attendance(
                                aid
                            ),
                        ).props("dense flat").tooltip("Back to automatic")
