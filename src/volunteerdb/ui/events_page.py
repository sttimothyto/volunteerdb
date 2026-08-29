"""Events: the scheduling pages.

/events is everyone's home base — your upcoming duties (with the
substitution flow), open substitute requests you could claim, and the
upcoming events on your teams; managers also create events here (with the
weekly repeat helper). /events/{id} is one event's workroom: slots with
sign-up/assignment, per-event RSVPs, and — once the event has ended — the
attendance record with manager-recorded exceptions.

Every action handler opens its own action_session and lets the service it calls
decide whether the actor may do it; mail goes out only AFTER the transaction
committed (send_email never raises), with links derived from the live request.

The detail page reads as an outline: it loads its data, computes what the page
needs, then draws the header and the slot list and calls one function per
section below it. Each section takes exactly what it draws and owns the handlers
only it uses, which is why they are module-level rather than nested — a section
that closes over nothing can be read on its own.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from nicegui import ui
from starlette.requests import Request

from .. import query_lang, throttle
from ..config import settings
from ..effects import Effect, SendMail, ThrottleHit
from ..env import current as current_env
from ..errors import NotFound, not_found, require
from ..fp import Err, Ok
from ..models import (
    Event,
    EventAssignment,
    EventRsvp,
    EventSlot,
    EventStatus,
    EventSubRequest,
    Volunteer,
)
from ..services import events as event_service
from ..services import gcal, mail
from ..services import task_force as task_force_service
from ..services import teams as team_service
from ..services import users as user_service
from . import calendar_grid, column_order
from .calendar_panel import subscribe_panel
from .context import PageCtx, page_ctx, run_command
from .date_input import date_input, time_input
from .layout import frame
from .volunteer_panel import VolunteerPanel, volunteer_link

# Substitute calls a single team may broadcast in a rolling day; the limit and
# its reasons live with the other families in throttle.LIMITS. Past it the
# request is still posted on /events, it is simply not announced.
SUB_REQUESTS_PER_TEAM_PER_DAY = throttle.LIMITS["sub-req"].hits


def _tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


def _status_badge(event) -> None:
    if event.status == EventStatus.cancelled.value:
        ui.badge("Cancelled", color="muted")
    elif event_service.is_past(event, now=current_env().clock.now()):
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


def _events_href(
    show_past: bool,
    team_filter: int | None,
    *,
    view: str | None = None,
    month: date | None = None,
) -> str:
    """/events with only the parameters that differ from the defaults, so the
    plain page stays the plain address. Every control on the page navigates
    through here — the past/upcoming toggle, the team filter, the calendar's
    view switch and month links — so each keeps the others' state."""
    parts = [
        p
        for p in (
            "past=1" if show_past else "",
            f"team={team_filter}" if team_filter else "",
            f"view={view}" if view and view != "mine" else "",
            f"month={month:%Y-%m}" if month else "",
        )
        if p
    ]
    return "/events?" + "&".join(parts) if parts else "/events"


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
            compiled = query_lang.compile_events(ast)
            if isinstance(compiled, Err):
                # inline, not a toast: this filter runs on every keystroke
                count.set_text(f"query error: {compiled.error.message}")
                return
            pred = compiled.value
            shown = [r for r in rows if pred(r)]
        table.rows = shown
        table.update()
        count.set_text(_event_count(len(shown), len(rows)))

    search.on_value_change(apply)


def _share_panel(base_url: str, event_id: int) -> None:
    """A "Share" button and the panel it opens: the event's link, and the
    caveat that the link asks for a sign-in, so a leader texting it out has
    to make sure everyone on the list has an account first.

    A native popover, not a dialog. Only the Copy button needs the
    websocket, and it is a convenience on top of an address anybody can
    select and copy by hand — so the panel opens, reads and closes on a page
    whose connection has dropped. The button is a QBtn like its neighbours;
    `popovertarget` falls through to its <button>, and the panel is
    addressed by the id NiceGUI gives every element.
    """
    url = f"{base_url}/events/{event_id}"
    with (
        ui.element("div")
        .props('popover="auto" role="dialog" aria-label="Share this event"')
        .classes("vdb-popover") as panel
    ):
        ui.label("Event link").classes("text-lg font-medium")
        ui.input(value=url).props(
            'readonly outlined dense aria-label="Event link"'
        ).classes("w-full").mark("share-url")
        ui.label(
            "Before you email or text this link out, make sure every "
            "volunteer has a VolunteerDB account — opening it asks for a "
            "sign-in, and the event is shown only to members of its team."
        ).classes("text-sm text-gray-500")
        with ui.row().classes("justify-end w-full gap-2"):
            copy = ui.button("Copy", icon="content_copy").props("dense flat")
            copy.on_click(lambda: (ui.clipboard.write(url), copy.set_text("Copied")))
            ui.button("Close").props(
                f'dense flat popovertarget="c{panel.id}" popovertargetaction="hide"'
            )
    ui.button("Share", icon="share").props(
        f'dense outline popovertarget="c{panel.id}"'
    ).mark("share-event")


async def _sub_request_dialog(assignment_id: int) -> None:
    """Open a substitution call; the policy mails the teammates who could
    take it.

    This is the widest fan-out in the app — one click mails every teammate not
    already serving, and the largest roster here is 28 people — so it is the
    one action rate-limited by volume rather than by abuse: see
    SUB_REQUESTS_PER_TEAM_PER_DAY (policy.py decides from the ledger)."""
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

        async def save() -> None:
            async def command(ctx: PageCtx):
                return await event_service.request_sub(
                    ctx.session,
                    ctx.actor,
                    assignment_id=assignment_id,
                    requested_by=ctx.actor.user.id,
                    note=note.value,
                    now=ctx.now,
                )

            def done(_sub, effects: tuple[Effect, ...], _report) -> None:
                # The request itself is never refused — it belongs on the
                # events page whether or not it is announced — but the blast
                # is capped: a team that has already sent its allowance today
                # gets the row and no mail, and the asker is told so plainly.
                dialog.close()
                capped = not any(isinstance(e, ThrottleHit) for e in effects)
                if capped:
                    ui.notify(
                        "Your request is posted on the Events page, but this team "
                        f"has already sent its {SUB_REQUESTS_PER_TEAM_PER_DAY} "
                        "substitute emails for today — nobody was mailed. Ask a "
                        "teammate directly, or try again tomorrow.",
                        color="warning",
                        multi_line=True,
                        timeout=10000,
                    )
                else:
                    mailed = sum(isinstance(e, SendMail) for e in effects)
                    ui.notify(
                        f"Asked {mailed} teammate(s) for a substitute",
                        color="positive",
                    )

            await run_command(command, on_ok=done)

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Ask the team", icon="campaign", on_click=save)
    dialog.open()


async def _substitute_dialog(assignment_id: int, options: dict[int, str]) -> None:
    """Hand a slot straight to a chosen teammate — no open call, no race."""
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Hand this slot to a teammate").classes("text-lg font-medium")
        ui.label(
            "Select this ONLY if there is prior agreement with the hand off target. "
            "They take the slot immediately and are emailed about it. The "
            "change is recorded: who made it, and when, goes into the log."
        ).classes("text-sm text-gray-500")
        pick = (
            ui.select(options, label="Who takes it?", with_input=True)
            .props("outlined dense")
            .classes("w-full")
        )

        async def save() -> None:
            if not pick.value:
                ui.notify("Pick a teammate first", color="warning")
                return

            async def command(ctx: PageCtx):
                return await event_service.substitute(
                    ctx.session,
                    ctx.actor,
                    assignment_id=assignment_id,
                    new_volunteer_id=pick.value,
                    acted_by=ctx.actor.user.id,
                    notify=ctx.env.notify,  # direct: the policy mails the incoming volunteer
                    now=ctx.now,
                )

            def done(value, _effects, _report) -> None:
                _assignment, _outgoing, incoming = value
                dialog.close()
                ui.notify(f"{incoming.full_name} now holds the slot", color="positive")

            await run_command(command, on_ok=done)

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

        async def save() -> None:
            text = (reason.value or "").strip()
            if not text:
                ui.notify("A reason is required", color="warning")
                return

            async def command(ctx: PageCtx):
                assignment = await event_service.get_assignment(
                    ctx.session, assignment_id
                )
                if assignment is None:
                    return not_found("assignment", assignment_id)
                # remove_assignment allows a manager too; taking YOURSELF off
                # is the flow this dialog serves, and the reason it collects
                if denied := require(
                    assignment.volunteer_id == ctx.actor.volunteer_id,
                    "take somebody else off their slot",
                ):
                    return denied
                return await event_service.remove_assignment(
                    ctx.session, ctx.actor, assignment_id, now=ctx.now, reason=text
                )

            def done(_event, _effects, _report) -> None:
                dialog.close()
                ui.notify(
                    "You're off the slot — the leaders have been told",
                    color="positive",
                )

            await run_command(command, on_ok=done)

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Take me off", on_click=save).props("color=negative")
    dialog.open()


async def _claim_sub(sub_request_id: int) -> None:
    async def command(ctx: PageCtx):
        return await event_service.claim_sub(
            ctx.session,
            ctx.actor,
            sub_request_id=sub_request_id,
            volunteer_id=ctx.actor.volunteer_id,
            now=ctx.now,
        )

    await run_command(
        command,
        on_ok=lambda _v, _e, _r: ui.notify(
            "The slot is yours — thank you!", color="positive"
        ),
    )


async def _withdraw_sub(sub_request_id: int) -> None:
    async def command(ctx: PageCtx):
        return await event_service.cancel_sub(
            ctx.session, ctx.actor, sub_request_id, now=ctx.now
        )

    await run_command(
        command,
        on_ok=lambda _v, _e, _r: ui.notify("Request withdrawn", color="positive"),
    )


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
                    f"{mail.event_when(hit.starts_at, hit.ends_at, tz=_tz())} · "
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
            start = time_input("Starts (HH:MM)", value="10:00").classes("w-36")
            end = time_input("Ends (HH:MM)", value="12:00").classes("w-36")
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

            async def lookalikes(ctx: PageCtx):
                return await event_service.similar_events(
                    ctx.session,
                    ctx.actor,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    repeat_until=until,
                    location=location.value,
                    tz=_tz(),
                )

            hits = await run_command(lookalikes, reload=False)
            if isinstance(hits, Err):
                return
            if hits.value and not await _confirm_similar(hits.value):
                return  # back to the still-open form

            async def command(ctx: PageCtx):
                return await event_service.create_event(
                    ctx.session,
                    ctx.actor,
                    team_id=team.value,
                    title=title.value or "",
                    starts_at=starts_at,
                    ends_at=ends_at,
                    description=description.value,
                    location=location.value,
                    slots=slots,
                    repeat_weekly_until=until,
                    created_by=ctx.actor.user.id,
                    tz=_tz(),
                    series_id=ctx.env.rng.uuid(),
                )

            def done(created, _effects, _report) -> None:
                dialog.close()
                if len(created) > 1:
                    ui.notify(f"{len(created)} events created", color="positive")
                ui.navigate.to(f"/events/{created[0].id}")

            await run_command(command, on_ok=done, reload=False)

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create event", icon="event", on_click=save)
    dialog.open()


@ui.page("/events")
async def events_page(
    request: Request, past: str = "", team: str = "", view: str = "", month: str = ""
):
    """The listing had two hardcoded modes — upcoming, or past-and-cancelled —
    while the API took a free `team_id`. `?team=` narrows to one ministry (and
    its sub-teams are separate rows, as they are separate teams), which is what
    a leader of several wants when they are looking at one of them.

    `?view=` picks the calendar's scope (mine, the default, or parish) and
    `?month=YYYY-MM` the month it shows; both are links, not widgets."""
    base_url = str(request.base_url).rstrip("/")
    show_past = past == "1"
    team_filter = int(team) if team.isdigit() else None
    cal_view = view if view in dict(calendar_grid.VIEWS) else "mine"
    async with page_ctx() as ctx:
        session, actor = ctx.session, ctx.actor
        duties = (
            await event_service.my_upcoming(
                session, actor.volunteer_id, now=current_env().clock.now()
            )
            if actor.volunteer_id is not None
            else []
        )
        claimable = await event_service.claimable_subs(
            session, actor, now=current_env().clock.now()
        )
        now = datetime.now(_tz())
        summaries = await event_service.list_events(
            session,
            actor,
            team_id=team_filter,
            from_=None if show_past else now,
            to=now if show_past else None,
            include_cancelled=show_past,
        )
        visible_teams = {s.event.team_id: s.path for s in summaries}
        calendar = await gcal.stored_calendar(session)
        cal_month = calendar_grid.parse_month(month, now.date())
        cal_from, cal_to = calendar_grid.window(cal_month, _tz())
        entries = (
            await event_service.calendar_entries(
                session, actor, scope=cal_view, from_=cal_from, to=cal_to
            )
        ).unwrap()
        # the personal feed address, minted the first time it is shown
        feed_token = (
            (
                await user_service.ensure_calendar_token(
                    session, actor.user.id, token=current_env().rng.token()
                )
            ).unwrap()
            if cal_view == "mine"
            else None
        )
        managed_options: dict[int, str] = {}
        if actor.can_create_events:
            tree = await team_service.tree(session)
            paths = tree.paths
            managed_options = {
                t.id: paths[t.id]
                for t in tree.teams
                if t.is_active and actor.can_manage_team(t.id)
            }
    if show_past:
        summaries = list(reversed(summaries))  # most recent past first

    # drawers must be direct children of page content, so build it before
    # entering frame (see ui/volunteer_panel.py)
    panel = VolunteerPanel("", base_url)
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
                            mail.event_when(
                                duty.event.starts_at, duty.event.ends_at, tz=_tz()
                            )
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
                                    _sub_request_dialog(aid)
                                ),
                            ).props("dense outline")

        if claimable:
            ui.label("Teammates need a substitute").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for c in claimable:
                    with ui.row().classes(
                        "w-full items-center gap-2 p-2 rounded bg-amber-50"
                    ):
                        volunteer_link(c.volunteer.full_name, c.volunteer.id, panel)
                        ui.label(f"needs a {c.slot.name} at").classes("text-sm")
                        ui.link(c.event.title, f"/events/{c.event.id}")
                        ui.label(
                            mail.event_when(
                                c.event.starts_at, c.event.ends_at, tz=_tz()
                            )
                        ).classes("text-sm text-gray-600")
                        if c.sub.note:
                            ui.label(f"“{c.sub.note}”").classes("text-sm text-gray-500")
                        ui.space()
                        ui.button(
                            "Take this slot",
                            icon="volunteer_activism",
                            on_click=lambda _, sid=c.sub.id: _claim_sub(sid),
                        ).props("dense outline")

        # the calendar: my duties or the whole parish, a month at a time.
        # Server-rendered HTML (ui/calendar_grid.py): a link changes the month
        # or the view, so it needs neither JavaScript nor the websocket
        def _cal_href(v: str, m: date) -> str:
            return _events_href(show_past, team_filter, view=v, month=m)

        with ui.row().classes("w-full items-center gap-3 flex-wrap mt-4"):
            ui.html(
                calendar_grid.view_switch(
                    cal_view,
                    {v: _cal_href(v, cal_month) for v, _ in calendar_grid.VIEWS},
                ),
                sanitize=False,
            ).mark("calendar-views")
            ui.space()
            subscribe_panel(
                view=cal_view,
                base_url=base_url,
                token=feed_token,
                calendar=calendar,
                is_admin=actor.is_admin,
            )
        ui.html(
            calendar_grid.month_grid(
                entries,
                cal_month,
                today=now.date(),
                tz=_tz(),
                prev_href=_cal_href(cal_view, calendar_grid.shift_month(cal_month, -1)),
                next_href=_cal_href(cal_view, calendar_grid.shift_month(cal_month, 1)),
                empty_note=(
                    "Nothing you are signed up for this month."
                    if cal_view == "mine"
                    else "No events this month."
                ),
            ),
            sanitize=False,
        ).classes("w-full").mark("calendar-grid")

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
            # one ministry at a time, for somebody who runs several. Offered only
            # when there is more than one team to choose between, and built from
            # the rows on screen rather than from every team the actor can see:
            # a filter that leads to an empty list is a worse control than none.
            if team_filter is not None or len(visible_teams) > 1:
                options = {0: "All teams"} | dict(
                    sorted(visible_teams.items(), key=lambda kv: kv[1])
                )
                if team_filter is not None and team_filter not in options:
                    options[team_filter] = "(filtered)"

                def _go(team_id: int | None) -> None:
                    """Keep the past/upcoming mode while the team changes, and
                    vice versa — the two controls are independent."""
                    ui.navigate.to(
                        _events_href(
                            show_past, team_id or None, view=cal_view, month=cal_month
                        )
                    )

                ui.select(
                    options,
                    value=team_filter or 0,
                    on_change=lambda e: _go(e.value),
                ).props("outlined dense options-dense").classes("w-52").mark(
                    "events-team-filter"
                )
            ui.button(
                "Show upcoming" if show_past else "Show past",
                on_click=lambda: ui.navigate.to(
                    _events_href(
                        not show_past, team_filter, view=cal_view, month=cal_month
                    )
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
            # a real link in the title cell (the teams page idiom), so the row
            # is reachable by keyboard; the row click stays for the mouse
            table.add_slot(
                "body-cell-title",
                '<q-td key="title" :props="props"><a :href="\'/events/\' + props.row.id" '
                'class="vdb-quiet" @click.stop>{{ props.row.title }}</a></q-td>',
            )
            table.add_slot(
                "body-cell-filled",
                # a <meter> where the capacity is finite: the fill reads at a
                # glance and as a value to a screen reader; ∞ stays words
                '<q-td key="filled" :props="props">'
                '<meter v-if="props.row.capacity_n !== null" class="vdb-meter" min="0" '
                ':max="props.row.capacity_n" :value="props.row.filled_n" '
                ":aria-label=\"props.row.filled + ' filled'\"></meter>"
                "{{ props.row.filled }}</q-td>",
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
            start = time_input("Starts (HH:MM)", value=f"{local_start:%H:%M}").classes(
                "w-36"
            )
            end = time_input("Ends (HH:MM)", value=f"{local_end:%H:%M}").classes("w-36")
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

        async def save() -> None:
            starts_at = _parse_local(day.value, start.value, "Start")
            ends_at = _parse_local(day.value, end.value, "End")
            if starts_at is None or ends_at is None:
                return

            async def command(ctx: PageCtx):
                return await event_service.update_event(
                    ctx.session,
                    ctx.actor,
                    event.id,
                    title=title.value or "",
                    description=description.value,
                    location=location.value,
                    starts_at=starts_at,
                    ends_at=ends_at,
                )

            def done(_value, _effects, _report) -> None:
                dialog.close()

            await run_command(command, on_ok=done, reload=True)

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
        # the name is the series-wide identity a copy-forward matches on, so
        # anything explanatory belongs here instead of in it
        description = (
            ui.input("Description (optional)")
            .props("outlined dense")
            .classes("w-full")
            .mark("slot-add-description")
        )

        async def save() -> None:

            async def command(ctx: PageCtx):
                return await event_service.add_slot(
                    ctx.session,
                    ctx.actor,
                    event_id,
                    name=name.value or "",
                    capacity=int(capacity.value) if capacity.value else None,
                    description=description.value,
                    now=ctx.now,
                )

            def done(_value, _effects, _report) -> None:
                dialog.close()

            await run_command(command, on_ok=done, reload=True)

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            # marked like slot-edit-save: the button that opens this dialog
            # carries the same label, so a test needs to name this one
            ui.button("Add slot", on_click=save).mark("slot-add-save")
    dialog.open()


def _edit_slot_dialog(
    slot_id: int,
    name_now: str,
    capacity_now: int | None,
    description_now: str | None = None,
) -> None:
    """Rename a slot, change how many it holds, or reword its description.

    Reachable over the API (PATCH /events/{id}/slots/{sid}) and nowhere in the
    GUI, so a mistyped slot name could only be fixed by deleting the slot —
    which needs it empty, and so meant taking the roster off it first. The
    description is here for the same reason: a note you can write once and
    never correct is worse than no note. Shrinking below what is already
    filled is refused by the service."""
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Edit slot").classes("text-lg font-medium")
        name = (
            ui.input("Slot name", value=name_now)
            .props("outlined dense")
            .classes("w-full")
            .mark("slot-edit-name")
        )
        capacity = (
            ui.number(
                "Capacity (blank = unlimited)",
                value=capacity_now,
                min=1,
                precision=0,
            )
            .props("outlined dense clearable")
            .classes("w-full")
            .mark("slot-edit-capacity")
        )
        description = (
            ui.input(
                "Description (optional) i.e. Google Doc w/ details",
                value=description_now or "",
            )
            .props("outlined dense")
            .classes("w-full")
            .mark("slot-edit-description")
        )

        async def save() -> None:

            async def command(ctx: PageCtx):
                return await event_service.update_slot(
                    ctx.session,
                    ctx.actor,
                    slot_id,
                    name=name.value or "",
                    capacity=int(capacity.value) if capacity.value else None,
                    description=description.value,
                    now=ctx.now,
                )

            def done(_value, _effects, _report) -> None:
                dialog.close()

            await run_command(command, on_ok=done, reload=True)

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).mark("slot-edit-save")
    dialog.open()


# --- detail-page sections ----------------------------------------------------
#
# One function per card or list on /events/{id}, each taking exactly what it
# draws and owning the handlers only it uses. They read top to bottom in the
# order the page renders them, and the page function below is the outline.
# NiceGUI's slot stack is dynamic, so a section called inside `with frame(...)`
# adds to that frame like inline code would.


def _collaboration_card(
    event_id: int,
    tf_view: task_force_service.TaskForceView | None,
    source_paths: list[str],
    collaborator_options: dict[int, str],
) -> None:
    """Add another team's roster to this event, or re-copy the ones already in.

    Manager-only, upcoming events only — the caller gates that."""

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

    async def _add_collaborator(team_id_value) -> None:
        if not team_id_value:
            ui.notify("Pick a team first", color="warning")
            return
        label = collaborator_options.get(team_id_value, "that team")
        if not await _confirm_add_collaborator(label):
            return

        async def command(ctx: PageCtx):
            return await task_force_service.add_collaborating_team(
                ctx.session,
                ctx.actor,
                event_id=event_id,
                source_team_id=team_id_value,
                created_by=ctx.actor.user.id,
                now=ctx.now,
                tz=_tz(),
            )

        await run_command(
            command,
            on_ok=lambda _v, _e, _r: ui.notify(
                "Team added — their roster can sign up now", color="positive"
            ),
        )

    async def _sync_rosters() -> None:

        async def command(ctx: PageCtx):
            return await task_force_service.refresh_rosters(
                ctx.session, ctx.actor, event_id
            )

        def done(value, _effects, _report) -> None:
            added = value
            ui.notify(f"Rosters synced — {added} member(s) added", color="positive")

        await run_command(command, on_ok=done, reload=True)

    with ui.card().classes("w-full gap-2 p-3"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("Collaboration").classes("font-medium")
            if tf_view is not None:
                ui.badge("task force", color="secondary")
            ui.space()
            if tf_view is not None:
                ui.button("Sync rosters", icon="sync", on_click=_sync_rosters).props(
                    "dense flat"
                ).tooltip(
                    "Re-copy the source rosters — people who joined a "
                    "staffing team since then join the task force"
                )
        if tf_view is not None:
            ui.label("Staffed by: " + " · ".join(source_paths)).classes(
                "text-sm text-gray-600"
            )
        else:
            ui.label(
                "Need another ministry for this event? After asking their "
                "ministry team leader for permission, adding a "
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


def _availability_card(event_id: int, my_rsvp: EventRsvp | None) -> None:
    """ "Can you serve?" — an answer, not a commitment; the assignment is that."""

    async def _rsvp(available: bool, note_value: str) -> None:

        async def command(ctx: PageCtx):
            return await event_service.set_rsvp(
                ctx.session,
                ctx.actor,
                event_id=event_id,
                volunteer_id=ctx.actor.volunteer_id,
                available=available,
                note=note_value,
                now=ctx.now,
            )

        await run_command(command, reload=True)

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


def _availability_answers(
    rsvps: list[tuple[EventRsvp, Volunteer]], panel: VolunteerPanel
) -> None:
    """The pool a manager assigns from."""
    ui.label("Availability answers").classes("text-lg font-medium mt-2")
    with ui.column().classes("w-full gap-1"):
        for rsvp, volunteer in rsvps:
            with ui.row().classes("w-full items-center gap-2 p-1"):
                volunteer_link(volunteer.full_name, volunteer.id, panel)
                ui.badge(
                    "available" if rsvp.available else "not available",
                    color="positive" if rsvp.available else "grey",
                )
                if rsvp.note:
                    ui.label(f"“{rsvp.note}”").classes("text-sm text-gray-500")


def _subs_wanted_section(
    eligible: list[tuple[EventSubRequest, EventAssignment]],
    slots: list[event_service.SlotView],
) -> None:
    """Open substitute calls this viewer could take over."""
    ui.label("Substitutes wanted").classes("text-lg font-medium mt-2")
    names = {v.id: v.full_name for sv in slots for _, v in sv.entries}
    slot_names = {sv.slot.id: sv.slot.name for sv in slots}
    for sub, a in eligible:
        with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-amber-50"):
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


def _attendance_section(
    event: Event,
    attendance: list[tuple[EventAssignment, EventSlot, Volunteer]],
    panel: VolunteerPanel,
) -> None:
    """Recorded after the event ends. Attendance is derived, so this section
    exists only to correct it: a row with no override shows the automatic
    answer, and Reset puts it back."""

    async def _save_attendance(
        assignment_id: int, attended_value: bool, hours_value
    ) -> None:
        try:
            hours = Decimal(str(hours_value)) if hours_value is not None else None
        except InvalidOperation:
            ui.notify("Hours must be a number", color="warning")
            return

        async def command(ctx: PageCtx):
            return await event_service.set_attendance(
                ctx.session,
                ctx.actor,
                assignment_id=assignment_id,
                attended=attended_value,
                hours=hours,
                now=ctx.now,
            )

        def done(_value, _effects, _report) -> None:
            ui.notify("Attendance saved", color="positive")

        await run_command(command, on_ok=done, reload=True)

    async def _clear_attendance(assignment_id: int) -> None:

        async def command(ctx: PageCtx):
            return await event_service.set_attendance(
                ctx.session,
                ctx.actor,
                assignment_id=assignment_id,
                attended=None,
                hours=None,
                now=ctx.now,
            )

        await run_command(command, reload=True)

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
            volunteer_link(volunteer.full_name, volunteer.id, panel, classes="w-48")
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
                on_click=lambda _, aid=assignment.id, b=box, h=hrs: _save_attendance(
                    aid, b.value, h.value
                ),
            ).props("dense flat")
            if overridden:
                ui.button(
                    "Reset",
                    on_click=lambda _, aid=assignment.id: _clear_attendance(aid),
                ).props("dense flat").tooltip("Back to automatic")


# --- detail-page actions -----------------------------------------------------
#
# Handlers the slot list drives. They take ids, not page state, and each opens
# its own session: the service they call is what authorizes the write.


def _signup_dialog(slot_id: int, slot_name: str, *, series: bool) -> None:
    """Confirm a sign-up, with the reminder stages to opt out of — and for a
    weekly series, the offer to take the later weeks in one go."""
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
        # 7 days unticked by default: it restates the notice that told you you
        # were scheduled, and the 24-hour one is what actually changes a day.
        # Still offered — some people plan a week out (models.EventAssignment).
        week = ui.checkbox("7 days before", value=False).props("dense")
        day = ui.checkbox("24 hours before", value=True).props("dense")

        async def save() -> None:
            async def command(ctx: PageCtx):
                if repeat is not None and repeat.value:
                    return await event_service.sign_up_series(
                        ctx.session,
                        ctx.actor,
                        slot_id=slot_id,
                        volunteer_id=ctx.actor.volunteer_id,
                        notify_7d=bool(week.value),
                        notify_24h=bool(day.value),
                        now=ctx.now,
                    )
                return await event_service.sign_up(
                    ctx.session,
                    ctx.actor,
                    slot_id=slot_id,
                    volunteer_id=ctx.actor.volunteer_id,
                    notify_7d=bool(week.value),
                    notify_24h=bool(day.value),
                    now=ctx.now,
                )

            def done(value, _effects, _report) -> None:
                # sign_up_series answers (assignment, SeriesSignupResult)
                result = value[1] if isinstance(value, tuple) else None
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

            await run_command(command, on_ok=done)

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Sign up", icon="person_add", on_click=save).mark(
                "signup-confirm"
            )
    dialog.open()


async def _withdraw(assignment_id: int) -> None:

    async def command(ctx: PageCtx):
        return await event_service.remove_assignment(
            ctx.session, ctx.actor, assignment_id, now=ctx.now
        )

    await run_command(command, reload=True)


async def _assign(slot_id: int, volunteer_id: int | None) -> None:
    if not volunteer_id:
        ui.notify("Pick a person first", color="warning")
        return

    async def command(ctx: PageCtx):
        return await event_service.assign(
            ctx.session,
            ctx.actor,
            slot_id=slot_id,
            volunteer_id=volunteer_id,
            assigned_by=ctx.actor.user.id,
            now=ctx.now,
        )

    await run_command(command, reload=True)


async def _delete_slot(slot_id: int) -> None:

    async def command(ctx: PageCtx):
        return await event_service.delete_slot(
            ctx.session, ctx.actor, slot_id, now=ctx.now
        )

    await run_command(command, reload=True)


async def _cancel_event(event_id: int) -> None:
    """Confirm, then cancel: the mail goes out from `_do_cancel`, after commit."""
    with ui.dialog() as confirm, ui.card().classes("w-96 gap-3"):
        ui.label(
            "Cancel this event? Everyone signed up is emailed, and open "
            "substitute requests are closed with it."
        )
        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Keep it", on_click=lambda: confirm.submit(False)).props("flat")
            ui.button("Yes, cancel it", on_click=lambda: confirm.submit(True)).props(
                "color=negative"
            )
    if not await confirm:
        return
    await _do_cancel(event_id)


async def _do_cancel(event_id: int) -> None:
    """The policy mails everyone signed up — unless the event was already
    over, when nobody needs mail about it."""

    async def command(ctx: PageCtx):
        return await event_service.cancel_event(
            ctx.session,
            ctx.actor,
            event_id,
            cancelled_by=ctx.actor.user.id,
            now=ctx.now,
        )

    await run_command(command)


@ui.page("/events/{event_id}")
async def event_detail_page(request: Request, event_id: int):
    base_url = str(request.base_url).rstrip("/")
    async with page_ctx() as ctx:
        session, actor = ctx.session, ctx.actor
        shown = await event_service.detail(session, actor, event_id)
        can_manage = am_member = am_assigned = False
        roster, attendance, tf_view = [], None, None
        collaborator_options: dict[int, str] = {}
        source_paths: list[str] = []
        if isinstance(shown, Ok):
            view = shown.value
            event = view.event
            can_manage = actor.can_manage_team(event.team_id)
            am_member = (
                actor.volunteer_id is not None
                and await event_service.is_member(
                    session, actor.volunteer_id, event.team_id
                )
            )
            am_assigned = actor.volunteer_id is not None and any(
                v.id == actor.volunteer_id for sv in view.slots for _, v in sv.entries
            )
            # members holding a slot need the roster too: the hand-off picker
            # shows names, which everyone past can_view_roster_names may see
            roster = (
                (await team_service.roster(session, actor, event.team_id)).unwrap()
                if can_manage or am_assigned
                else []
            )
            attendance = (
                (await event_service.attendance_rows(session, actor, event_id)).unwrap()
                if can_manage
                and event_service.is_past(event, now=ctx.now)
                and event.status == EventStatus.scheduled.value
                else None
            )
            tf_view = await task_force_service.get_for_event(session, event_id)
            if can_manage:
                tree = await team_service.tree(session)
                paths = tree.paths
                staffing = (
                    {t.id for t in tf_view.sources} if tf_view else {event.team_id}
                )
                if tf_view:
                    staffing.add(tf_view.team_id)
                collaborator_options = {
                    t.id: paths[t.id]
                    for t in tree.teams
                    if t.is_active and t.id not in staffing
                }
                source_paths = (
                    [paths.get(t.id, t.name) for t in tf_view.sources]
                    if tf_view
                    else []
                )
    match shown:
        case Err(NotFound()):
            with frame("Event not found", actor):
                ui.label(f"No event with id {event_id}.")
            return
        case Err():
            # the service decides; the page only chooses how to say it, and
            # a whole page reads better than a toast on an empty frame
            with frame("Events", actor):
                ui.label("This event is visible to the members of its team.").classes(
                    "text-gray-500"
                )
            return
    view = shown.value
    event = view.event

    upcoming = (
        event.status == EventStatus.scheduled.value
        and not event_service.is_past(event, now=ctx.now)
    )
    sub_wanted = {a.id: sub for sub, a in view.open_subs}
    rsvp_by_vid = {v.id: r for r, v in view.rsvps}
    my_rsvp = rsvp_by_vid.get(actor.volunteer_id) if actor.volunteer_id else None
    my_assignment = next(
        (a for sv in view.slots for a, v in sv.entries if v.id == actor.volunteer_id),
        None,
    )
    assigned_vids = {v.id for sv in view.slots for _, v in sv.entries}
    series = event.series_id is not None

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

    panel = VolunteerPanel("", base_url)
    with frame(event.title, actor):
        with ui.row().classes("w-full items-center gap-2"):
            ui.link(view.path, f"/teams/{event.team_id}").classes("font-medium")
            _status_badge(event)
            ui.label(mail.event_when(event.starts_at, event.ends_at, tz=_tz())).classes(
                "text-sm text-gray-600"
            )
            if event.location:
                ui.label(f"· {event.location}").classes("text-sm text-gray-600")
            ui.space()
            _share_panel(base_url, event_id)
            if can_manage and event.status == EventStatus.scheduled.value:
                ui.button(
                    "Edit", icon="edit", on_click=lambda: _edit_event_dialog(event)
                ).props("dense outline")
                ui.button(
                    "Cancel event",
                    on_click=lambda: _cancel_event(event_id),
                ).props("dense outline color=negative")
        if event.description:
            ui.label(event.description).classes("text-sm text-gray-600")

        if can_manage and upcoming:
            _collaboration_card(event_id, tf_view, source_paths, collaborator_options)

        if am_member and upcoming:
            _availability_card(event_id, my_rsvp)

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
                                _signup_dialog(sid, sn, series=series)
                            ),
                        ).props("dense outline")
                    if can_manage and upcoming:
                        ui.button(
                            icon="edit",
                            on_click=lambda _, sid=slot.id, sn=slot.name, sc=slot.capacity, sd=slot.description: (
                                _edit_slot_dialog(sid, sn, sc, sd)
                            ),
                        ).props("dense flat").mark(f"slot-edit-{slot.id}").tooltip(
                            "Rename this slot, change how many it holds, or "
                            "reword its description"
                        )
                    if can_manage and upcoming and not sv.entries:
                        ui.button(
                            icon="delete",
                            on_click=lambda _, sid=slot.id: _delete_slot(sid),
                        ).props("dense flat").tooltip("Remove this empty slot")
                if slot.description:
                    ui.label(slot.description).classes("text-sm text-gray-600")
                for assignment, volunteer in sv.entries:
                    with ui.row().classes(
                        "w-full items-center gap-2 p-1 rounded hover:bg-gray-100"
                    ):
                        volunteer_link(volunteer.full_name, volunteer.id, panel)
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
                                    _sub_request_dialog(aid)
                                ),
                            ).props("dense outline")
                        if upcoming and volunteer.id == actor.volunteer_id:
                            # handing off with an open sub call cancels the call
                            ui.button(
                                "Hand off",
                                icon="swap_horiz",
                                on_click=lambda _, aid=assignment.id: (
                                    _substitute_dialog(
                                        aid, picker_options(assigned_vids)
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
            _availability_answers(view.rsvps, panel)

        eligible_subs = [
            (sub, a)
            for sub, a in view.open_subs
            if am_member
            and upcoming
            and a.volunteer_id != actor.volunteer_id
            and actor.volunteer_id not in assigned_vids
        ]
        if eligible_subs:
            _subs_wanted_section(eligible_subs, view.slots)

        if attendance is not None:
            _attendance_section(event, attendance, panel)
