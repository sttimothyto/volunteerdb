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

from ..config import settings
from ..models import EventSlot, EventStatus, EventSubRequest, Volunteer
from ..permissions import require
from ..services import events as event_service
from ..services import interest as interest_service
from ..services import mail
from ..services import teams as team_service
from .context import action_session, notify_errors, page_session
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
                require(
                    assignment.volunteer_id == actor.volunteer_id
                    or actor.can_manage_team(
                        (await event_service.get(session, assignment.event_id)).team_id
                    ),
                    "ask for a substitute for someone else",
                )
                sub = await event_service.request_sub(
                    session,
                    assignment_id=assignment_id,
                    requested_by=actor.user.id,
                    note=note.value,
                )
                view = await event_service.detail(session, assignment.event_id)
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


@notify_errors
async def _claim_sub(sub_request_id: int) -> None:
    async with action_session() as (session, actor):
        sub, assignment, asker = await event_service.claim_sub(
            session, sub_request_id=sub_request_id, volunteer_id=actor.volunteer_id
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
        assignment = await event_service.get_assignment(session, sub.assignment_id)
        event = (
            await event_service.get(session, assignment.event_id)
            if assignment
            else None
        )
        require(
            (assignment is not None and assignment.volunteer_id == actor.volunteer_id)
            or (event is not None and actor.can_manage_team(event.team_id)),
            "withdraw someone else's substitute request",
        )
        await event_service.cancel_sub(session, sub_request_id)
    ui.notify("Request withdrawn", color="positive")
    ui.navigate.reload()


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
            day = (
                ui.input("Date (YYYY-MM-DD)", value=str(tomorrow))
                .props("outlined dense")
                .classes("grow")
            )
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
        repeat = (
            ui.input("Repeat weekly until (YYYY-MM-DD, optional)")
            .props("outlined dense clearable")
            .classes("w-full")
        )

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
                require(actor.can_manage_team(team.value), "manage this team's events")
                created = await event_service.create_event(
                    session,
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

        with ui.row().classes("w-full items-center mt-4"):
            ui.label(
                ("Past events" if show_past else "Upcoming events")
                + (" (all teams)" if actor.is_admin else " on your teams")
            ).classes("text-lg font-medium")
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
                    "filled": f"{s.filled}/{s.capacity if s.capacity is not None else '∞'}",
                    "you": you,
                }
            )
        if rows:
            table = ui.table(
                columns=[
                    {"name": "when", "label": "When", "field": "when"},
                    {"name": "title", "label": "Event", "field": "title"},
                    {"name": "team", "label": "Team", "field": "team"},
                    {"name": "filled", "label": "Filled", "field": "filled"},
                    {"name": "you", "label": "You", "field": "you"},
                ],
                rows=rows,
                row_key="id",
                pagination=0,
            ).classes("w-full vdb-clickable-rows")
            table.on("rowClick", lambda e: ui.navigate.to(f"/events/{e.args[1]['id']}"))
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
            day = (
                ui.input("Date (YYYY-MM-DD)", value=str(local_start.date()))
                .props("outlined dense")
                .classes("grow")
            )
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
                require(
                    actor.can_manage_team(current.team_id),
                    "manage this team's events",
                )
                await event_service.update_event(
                    session,
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
                require(
                    actor.can_manage_team(current.team_id),
                    "manage this team's events",
                )
                await event_service.add_slot(
                    session,
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
            view = await event_service.detail(session, event_id)
        except LookupError:
            with frame("Event not found", actor):
                ui.label(f"No event with id {event_id}.")
            return
        event = view.event
        if not actor.can_view_roster_names(event.team_id):
            with frame("Events", actor):
                ui.label("This event is visible to the members of its team.").classes(
                    "text-gray-500"
                )
            return
        can_manage = actor.can_manage_team(event.team_id)
        am_member = actor.volunteer_id is not None and await event_service.is_member(
            session, actor.volunteer_id, event.team_id
        )
        roster = await team_service.roster(session, event.team_id) if can_manage else []
        attendance = (
            await event_service.attendance_rows(session, event_id)
            if can_manage
            and event_service.is_past(event)
            and event.status == EventStatus.scheduled.value
            else None
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
                event_id=event_id,
                volunteer_id=actor.volunteer_id,
                available=available,
                note=note_value,
            )
        ui.navigate.reload()

    @notify_errors
    async def _signup(slot_id: int) -> None:
        async with action_session() as (session, actor):
            await event_service.sign_up(
                session, slot_id=slot_id, volunteer_id=actor.volunteer_id
            )
        ui.notify("You're on the list", color="positive")
        ui.navigate.reload()

    @notify_errors
    async def _withdraw(assignment_id: int) -> None:
        async with action_session() as (session, actor):
            assignment = await event_service.get_assignment(session, assignment_id)
            if assignment is None:
                raise LookupError("assignment vanished")
            current = await event_service.get(session, assignment.event_id)
            require(
                assignment.volunteer_id == actor.volunteer_id
                or (current is not None and actor.can_manage_team(current.team_id)),
                "change other people's assignments",
            )
            await event_service.remove_assignment(session, assignment_id)
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
            require(actor.can_manage_team(current.team_id), "manage this team's events")
            await event_service.assign(
                session,
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
            require(actor.can_manage_team(current.team_id), "manage this team's events")
            await event_service.delete_slot(session, slot_id)
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
            require(actor.can_manage_team(current.team_id), "manage this team's events")
            was_upcoming = not event_service.is_past(current)
            cancelled, emails = await event_service.cancel_event(
                session, event_id, cancelled_by=actor.user.id
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
            require(actor.can_manage_team(current.team_id), "record attendance")
            await event_service.set_attendance(
                session,
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
            require(actor.can_manage_team(current.team_id), "record attendance")
            await event_service.set_attendance(
                session, assignment_id=assignment_id, attended=None, hours=None
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
            if can_manage and event.status == EventStatus.scheduled.value:
                ui.button(
                    "Edit", icon="edit", on_click=lambda: _edit_event_dialog(event)
                ).props("dense outline")
                ui.button("Cancel event", on_click=_cancel_event).props(
                    "dense outline color=negative"
                )
        if event.description:
            ui.label(event.description).classes("text-sm text-gray-600")

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
                            on_click=lambda _, sid=slot.id: _signup(sid),
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
                        if upcoming and (
                            can_manage or volunteer.id == actor.volunteer_id
                        ):
                            ui.button(
                                "Withdraw"
                                if volunteer.id == actor.volunteer_id
                                else "Remove",
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
