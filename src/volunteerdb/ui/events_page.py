"""Events: the scheduling pages.

/events is everyone's home base — your upcoming duties (with the
substitution flow), open substitute requests you could claim, and the
upcoming events on your teams; managers also create events here (with the
weekly repeat helper). /events/{id} is one event's workroom: slots with
sign-up/assignment, per-event RSVPs, and — once the event has ended — the
attendance record with manager-recorded exceptions.

Every action handler runs its command in its own unit of work and lets the
service it calls decide whether the actor may do it; mail goes out only
AFTER the transaction committed (the mailer never raises), with links
derived from the live request.

Both pages read as outlines. The listing parses its address, loads what it
lists, then draws one section per block. The workroom loads
readmodels.event_workroom -- the event, and what this reader may do there
-- and draws the header, the slot list and one call per section below it.
Sections, dialogs and handlers are module-level and take ids and the rows
they draw, so each can be read without the page around it.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from nicegui import ui

from .. import query_lang, throttle, timefmt
from ..effects import Effect, SendMail, ThrottleHit
from ..env import current as current_env
from ..errors import NotFound, not_found, require
from ..fp import Err, expect
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
from ..services import gcal, readmodels
from ..services import task_force as task_force_service
from ..services import teams as team_service
from ..services import users as user_service
from ..services.events import CalendarEntry, ClaimableSub, EventSummary, MyDuty
from ..services.readmodels import EventWorkroom
from . import calendar_grid, column_order
from .calendar_panel import subscribe_panel
from .context import PageCtx, flash, page_ctx, run_command, warn
from .date_input import date_input, time_input
from .forms import WIDE, actions, confirm, dialog_card
from .layout import frame
from .tables import count_text, wire_search
from .volunteer_panel import VolunteerPanel, volunteer_link

# Substitute calls a single team may broadcast in a rolling day; the limit and
# its reasons live with the other families in throttle.LIMITS. Past it the
# request is still posted on /events, it is simply not announced.
SUB_REQUESTS_PER_TEAM_PER_DAY = throttle.LIMITS["sub-req"].hits


def _tz() -> ZoneInfo:
    return current_env().tz


def _status_badge(event: Event, *, now: datetime, tz: ZoneInfo) -> None:
    if event.status == EventStatus.cancelled.value:
        ui.badge("Cancelled", color="muted")
    elif event_service.is_past(event, now=now):
        ui.badge("Past", color="purple")
    else:
        ui.badge(f"{event.starts_at.astimezone(tz):%a %b %-d}", color="primary")


def _parse_local(day_s: str, time_s: str, what: str) -> datetime | None:
    try:
        return datetime.combine(
            date.fromisoformat(day_s or ""), time.fromisoformat(time_s or ""), _tz()
        )
    except ValueError:
        warn(f"{what}: use YYYY-MM-DD and HH:MM")
        return None


# --- the listing's address ---------------------------------------------------


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


@dataclass(frozen=True)
class Listing:
    """What /events was asked for, as values: the two hardcoded modes
    (upcoming, or past-and-cancelled), one ministry or all, and the
    calendar's scope and month. `href` renders it back with a change
    applied, which is how each control keeps the others' state."""

    show_past: bool
    team_filter: int | None
    view: str  # the calendar's scope: mine or parish
    month: date  # the calendar's month, as its first day

    @classmethod
    def parse(
        cls, past: str, team: str, view: str, month: str, *, today: date
    ) -> "Listing":
        return cls(
            show_past=past == "1",
            team_filter=int(team) if team.isdigit() else None,
            view=view if view in dict(calendar_grid.VIEWS) else "mine",
            month=calendar_grid.parse_month(month, today),
        )

    def href(self, **changes) -> str:
        """The address of this listing with `changes` (field names) applied."""
        it = replace(self, **changes)
        return _events_href(it.show_past, it.team_filter, view=it.view, month=it.month)


def _matching_events(rows: list[dict], text: str) -> list[dict]:
    """The rows whose title, team, location or date contains `text`."""
    return [
        r
        for r in rows
        if any(
            text in (r[key] or "").lower()
            for key in ("title", "team", "location", "when")
        )
    ]


# --- the workroom's dialogs and actions ----------------------------------------
#
# Each takes ids, not page state, and opens its own unit of work through
# run_command: the service it calls is what authorizes the write.


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
    with dialog_card("Ask for a substitute") as dialog:
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
                    requested_by=ctx.actor.account.id,
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
                    flash(
                        "Your request is posted on the Events page, but this team "
                        f"has already sent its {SUB_REQUESTS_PER_TEAM_PER_DAY} "
                        "substitute emails for today — nobody was mailed. Ask a "
                        "teammate directly, or try again tomorrow.",
                        kind="warning",
                        multi_line=True,
                    )
                else:
                    mailed = sum(isinstance(e, SendMail) for e in effects)
                    flash(f"Asked {mailed} teammate(s) for a substitute")

            await run_command(command, on_ok=done)

        actions(dialog, "Ask the team", save, icon="campaign")
    dialog.open()


async def _substitute_dialog(assignment_id: int, options: dict[int, str]) -> None:
    """Hand a slot straight to a chosen teammate — no open call, no race."""
    with dialog_card("Hand this slot to a teammate") as dialog:
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
                warn("Pick a teammate first")
                return

            async def command(ctx: PageCtx):
                return await event_service.substitute(
                    ctx.session,
                    ctx.actor,
                    assignment_id=assignment_id,
                    new_volunteer_id=pick.value,
                    acted_by=ctx.actor.account.id,
                    notify=ctx.env.notify,  # direct: the policy mails the incoming volunteer
                    now=ctx.now,
                )

            def done(value, _effects, _report) -> None:
                _assignment, _outgoing, incoming = value
                dialog.close()
                flash(f"{incoming.full_name} now holds the slot")

            await run_command(command, on_ok=done)

        actions(dialog, "Hand it over", save, icon="swap_horiz")
    dialog.open()


async def _self_removal_dialog(assignment_id: int) -> None:
    """Take yourself off a slot, telling the leaders why."""
    with dialog_card("Take yourself off this slot") as dialog:
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
                warn("A reason is required")
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
                flash("You're off the slot — the leaders have been told")

            await run_command(command, on_ok=done)

        actions(dialog, "Take me off", save, danger=True)
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

    await run_command(command, success="The slot is yours — thank you!")


async def _withdraw_sub(sub_request_id: int) -> None:
    async def command(ctx: PageCtx):
        return await event_service.cancel_sub(
            ctx.session, ctx.actor, sub_request_id, now=ctx.now
        )

    await run_command(command, success="Request withdrawn")


async def _confirm_similar(hits: list[event_service.SimilarEvent]) -> bool:
    """The double-booking warning: advisory, never a block. A masked title
    means the colliding event belongs to a team outside the creator's view —
    the when/where is the warning; the details stay theirs."""
    with dialog_card("Possible double booking", width=WIDE) as dialog:
        ui.label(
            "Something similar is already on the calendar at that location "
            "on the same day:"
        ).classes("text-sm text-gray-500")
        for hit in hits:
            with ui.column().classes("w-full gap-0 p-2 rounded bg-amber-50"):
                ui.label(hit.title or "Another team's event").classes("font-medium")
                ui.label(
                    f"{timefmt.event_when(hit.starts_at, hit.ends_at, tz=_tz())} · "
                    f"{hit.location} · {hit.team_path}"
                ).classes("text-sm text-gray-600")
        actions(
            dialog,
            "Create anyway",
            lambda: dialog.submit(True),
            cancel="Go back",
            on_cancel=lambda: dialog.submit(False),
        ).props("color=warning")
    return bool(await dialog)


def _new_event_dialog(managed_options: dict[int, str]) -> None:
    with dialog_card("New event", width=WIDE) as dialog:
        team = (
            ui.select(managed_options, label="Team", with_input=True)
            .props("outlined dense")
            .classes("w-full")
        )
        title = ui.input("Title").props("outlined dense").classes("w-full")
        tomorrow = current_env().today() + timedelta(days=1)
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
                warn("Pick the team")
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
                    warn("Repeat until: use YYYY-MM-DD")
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
                    tz=ctx.env.tz,
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
                    created_by=ctx.actor.account.id,
                    tz=ctx.env.tz,
                    series_id=ctx.env.rng.uuid(),
                )

            def done(created, _effects, _report) -> None:
                dialog.close()
                flash(
                    f"{len(created)} events created"
                    if len(created) > 1
                    else "Event created"
                )
                ui.navigate.to(f"/events/{created[0].id}")

            await run_command(command, on_ok=done, reload=False)

        actions(dialog, "Create event", save, icon="event")
    dialog.open()


# --- the listing's sections ----------------------------------------------------


def _duties_section(duties: list[MyDuty], tz: ZoneInfo) -> None:
    """Your upcoming duties, each with its substitution control."""
    ui.label("Your upcoming duties").classes("text-lg font-medium")
    with ui.column().classes("w-full gap-1"):
        for duty in duties:
            with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-gray-50"):
                ui.link(duty.event.title, f"/events/{duty.event.id}").classes(
                    "font-medium"
                )
                ui.badge(duty.slot.name)
                ui.label(
                    timefmt.event_when(duty.event.starts_at, duty.event.ends_at, tz=tz)
                ).classes("text-sm text-gray-600")
                ui.space()
                if duty.open_sub is not None:
                    ui.badge("sub wanted", color="warning")
                    ui.button(
                        "Withdraw request",
                        on_click=lambda _, sid=duty.open_sub.id: _withdraw_sub(sid),
                    ).props("dense flat")
                else:
                    ui.button(
                        "Need a sub",
                        icon="campaign",
                        on_click=lambda _, aid=duty.assignment.id: _sub_request_dialog(
                            aid
                        ),
                    ).props("dense outline")


def _claimable_section(
    claimable: list[ClaimableSub], panel: VolunteerPanel, tz: ZoneInfo
) -> None:
    """Teammates' open calls this reader could answer."""
    ui.label("Teammates need a substitute").classes("text-lg font-medium mt-4")
    with ui.column().classes("w-full gap-1"):
        for c in claimable:
            with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-amber-50"):
                volunteer_link(c.volunteer.full_name, c.volunteer.id, panel)
                ui.label(f"needs a {c.slot.name} at").classes("text-sm")
                ui.link(c.event.title, f"/events/{c.event.id}")
                ui.label(
                    timefmt.event_when(c.event.starts_at, c.event.ends_at, tz=tz)
                ).classes("text-sm text-gray-600")
                if c.sub.note:
                    ui.label(f"“{c.sub.note}”").classes("text-sm text-gray-500")
                ui.space()
                ui.button(
                    "Take this slot",
                    icon="volunteer_activism",
                    on_click=lambda _, sid=c.sub.id: _claim_sub(sid),
                ).props("dense outline")


def _calendar_section(
    listing: Listing,
    entries: list[CalendarEntry],
    *,
    today: date,
    tz: ZoneInfo,
    feed_token: str | None,
    calendar: dict | None,
    is_admin: bool,
    base_url: str,
) -> None:
    """The calendar: my duties or the whole parish, a month at a time.

    Server-rendered HTML (ui/calendar_grid.py): a link changes the month or
    the view, so it needs neither JavaScript nor the websocket."""
    with ui.row().classes("w-full items-center gap-3 flex-wrap mt-4"):
        ui.html(
            calendar_grid.view_switch(
                listing.view,
                {v: listing.href(view=v) for v, _ in calendar_grid.VIEWS},
            ),
            sanitize=False,
        ).mark("calendar-views")
        ui.space()
        subscribe_panel(
            view=listing.view,
            base_url=base_url,
            token=feed_token,
            calendar=calendar,
            is_admin=is_admin,
        )
    ui.html(
        calendar_grid.month_grid(
            entries,
            listing.month,
            today=today,
            tz=tz,
            prev_href=listing.href(month=calendar_grid.shift_month(listing.month, -1)),
            next_href=listing.href(month=calendar_grid.shift_month(listing.month, 1)),
            empty_note=(
                "Nothing you are signed up for this month."
                if listing.view == "mine"
                else "No events this month."
            ),
        ),
        sanitize=False,
    ).classes("w-full").mark("calendar-grid")


def _event_rows(summaries: list[EventSummary], tz: ZoneInfo) -> list[dict]:
    """One table row per event; numeric twins beside the pretty "3/∞" so the
    column sorts and the query language can compare."""
    rows = []
    for s in summaries:
        local = s.event.starts_at.astimezone(tz)
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
                "filled_n": s.filled,
                "capacity_n": s.capacity,
                "you": you,
            }
        )
    return rows


def _listing_controls(
    listing: Listing,
    *,
    has_rows: bool,
    visible_teams: dict[int, str],
    managed_options: dict[int, str],
    is_admin: bool,
) -> ui.input | None:
    """The row above the table: its title, the search box (returned, for the
    table to wire), the team filter, the past/upcoming toggle and, for a
    manager, the New event button."""
    with ui.row().classes("w-full items-center mt-4"):
        ui.label(
            ("Past events" if listing.show_past else "Upcoming events")
            + (" (all teams)" if is_admin else " on your teams")
        ).classes("text-lg font-medium")
        # the search box grows into the free space and holds the buttons
        # against the right edge (the teams-page idiom); with nothing to
        # search the spacer takes over that job
        search = (
            ui.input("Search events…")
            .props("outlined dense clearable debounce=200")
            .classes("grow")
            if has_rows
            else None
        )
        if search is None:
            ui.space()
        # one ministry at a time, for somebody who runs several. Offered only
        # when there is more than one team to choose between, and built from
        # the rows on screen rather than from every team the actor can see:
        # a filter that leads to an empty list is a worse control than none.
        if listing.team_filter is not None or len(visible_teams) > 1:
            options = {0: "All teams"} | dict(
                sorted(visible_teams.items(), key=lambda kv: kv[1])
            )
            if listing.team_filter is not None and listing.team_filter not in options:
                options[listing.team_filter] = "(filtered)"
            # the past/upcoming mode survives a team change, and vice versa:
            # the two controls are independent
            ui.select(
                options,
                value=listing.team_filter or 0,
                on_change=lambda e: ui.navigate.to(
                    listing.href(team_filter=e.value or None)
                ),
            ).props("outlined dense options-dense").classes("w-52").mark(
                "events-team-filter"
            )
        ui.button(
            "Show upcoming" if listing.show_past else "Show past",
            on_click=lambda: ui.navigate.to(
                listing.href(show_past=not listing.show_past)
            ),
        ).props("dense flat no-caps")
        if managed_options:
            ui.button(
                "New event",
                icon="event",
                on_click=lambda: _new_event_dialog(managed_options),
            ).props("dense outline")
    return search


EVENT_COLUMNS = [
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


def _events_table(rows: list[dict], *, show_past: bool, search: ui.input) -> None:
    """The listing itself, its count line, and the search box wired to it."""
    columns = column_order.apply_saved_order("events", EVENT_COLUMNS)
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
    count = ui.label(count_text(len(rows), None, "event")).classes(
        "text-sm text-gray-500"
    )
    wire_search(
        search,
        count,
        table,
        rows,
        noun="event",
        compile=query_lang.compile_events,
        text_filter=_matching_events,
    )


@ui.page("/events")
async def events_page(past: str = "", team: str = "", view: str = "", month: str = ""):
    """The listing had two hardcoded modes — upcoming, or past-and-cancelled —
    while the API took a free `team_id`. `?team=` narrows to one ministry (and
    its sub-teams are separate rows, as they are separate teams), which is what
    a leader of several wants when they are looking at one of them.

    `?view=` picks the calendar's scope (mine, the default, or parish) and
    `?month=YYYY-MM` the month it shows; both are links, not widgets."""
    async with page_ctx() as ctx:
        session, actor, tz = ctx.session, ctx.actor, ctx.env.tz
        now = ctx.now.astimezone(tz)
        listing = Listing.parse(past, team, view, month, today=now.date())
        duties = (
            await event_service.my_upcoming(session, actor.volunteer_id, now=ctx.now)
            if actor.volunteer_id is not None
            else []
        )
        claimable = await event_service.claimable_subs(session, actor, now=ctx.now)
        summaries = await event_service.list_events(
            session,
            actor,
            team_id=listing.team_filter,
            from_=None if listing.show_past else now,
            to=now if listing.show_past else None,
            include_cancelled=listing.show_past,
        )
        visible_teams = {s.event.team_id: s.path for s in summaries}
        calendar = await gcal.stored_calendar(session)
        cal_from, cal_to = calendar_grid.window(listing.month, tz)
        entries = expect(
            await event_service.calendar_entries(
                session, actor, scope=listing.view, from_=cal_from, to=cal_to
            )
        )
        # the personal feed address, minted the first time it is shown
        feed_token = (
            (
                await user_service.ensure_calendar_token(
                    session, actor.account.id, token=ctx.env.rng.token()
                )
            ).unwrap_or([])
            if listing.view == "mine"
            else None
        )
        managed_options: dict[int, str] = {}
        if actor.can_create_events:
            tree = await team_service.tree(session)
            managed_options = {
                t.id: tree.paths[t.id]
                for t in tree.teams
                if t.is_active and actor.can_manage_team(t.id)
            }
    if listing.show_past:
        summaries = list(reversed(summaries))  # most recent past first
    rows = _event_rows(summaries, tz)

    # drawers must be direct children of page content, so build it before
    # entering frame (see ui/volunteer_panel.py)
    panel = VolunteerPanel("", ctx.base_url)
    with frame("Events", actor):
        if duties:
            _duties_section(duties, tz)
        if claimable:
            _claimable_section(claimable, panel, tz)
        _calendar_section(
            listing,
            entries,
            today=now.date(),
            tz=tz,
            feed_token=feed_token,
            calendar=calendar,
            is_admin=actor.is_admin,
            base_url=ctx.base_url,
        )
        search = _listing_controls(
            listing,
            has_rows=bool(rows),
            visible_teams=visible_teams,
            managed_options=managed_options,
            is_admin=actor.is_admin,
        )
        if search is not None:
            _events_table(rows, show_past=listing.show_past, search=search)
        else:
            ui.label(
                "Nothing scheduled yet."
                + (
                    ""
                    if not managed_options
                    else " Create the first event with the button above."
                )
            ).classes("text-gray-500")


# --- the workroom's dialogs ----------------------------------------------------


def _edit_event_dialog(event: Event) -> None:
    local_start = event.starts_at.astimezone(_tz())
    local_end = event.ends_at.astimezone(_tz())
    with dialog_card("Edit event", width=WIDE) as dialog:
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

            await run_command(command, on_ok=done, reload=True, success="Event saved")

        actions(dialog, "Save", save)
    dialog.open()


def _add_slot_dialog(event_id: int) -> None:
    with dialog_card("Add a slot") as dialog:
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

            await run_command(command, on_ok=done, reload=True, success="Slot added")

        # marked like slot-edit-save: the button that opens this dialog
        # carries the same label, so a test needs to name this one
        actions(dialog, "Add slot", save, marker="slot-add-save")
    dialog.open()


def _edit_slot_dialog(slot: EventSlot) -> None:
    """Rename a slot, change how many it holds, or reword its description.

    Reachable over the API (PATCH /events/{id}/slots/{sid}) and nowhere in the
    GUI, so a mistyped slot name could only be fixed by deleting the slot —
    which needs it empty, and so meant taking the roster off it first. The
    description is here for the same reason: a note you can write once and
    never correct is worse than no note. Shrinking below what is already
    filled is refused by the service."""
    with dialog_card("Edit slot") as dialog:
        name = (
            ui.input("Slot name", value=slot.name)
            .props("outlined dense")
            .classes("w-full")
            .mark("slot-edit-name")
        )
        capacity = (
            ui.number(
                "Capacity (blank = unlimited)",
                value=slot.capacity,
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
                value=slot.description or "",
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
                    slot.id,
                    name=name.value or "",
                    capacity=int(capacity.value) if capacity.value else None,
                    description=description.value,
                    now=ctx.now,
                )

            def done(_value, _effects, _report) -> None:
                dialog.close()

            await run_command(command, on_ok=done, reload=True, success="Slot saved")

        actions(dialog, "Save", save, marker="slot-edit-save")
    dialog.open()


def _signup_dialog(slot_id: int, slot_name: str, *, series: bool) -> None:
    """Confirm a sign-up, with the reminder stages to opt out of — and for a
    weekly series, the offer to take the later weeks in one go."""
    with dialog_card(f"Sign up — {slot_name}") as dialog:
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
                    flash("You're on the list")
                else:
                    skipped = result.skipped_full + result.skipped_conflict
                    flash(
                        f"You're on the list — this week plus {result.joined} more"
                        + (f", {skipped} week(s) skipped" if skipped else "")
                    )

            await run_command(command, on_ok=done)

        actions(dialog, "Sign up", save, icon="person_add", marker="signup-confirm")
    dialog.open()


# --- the workroom's actions ----------------------------------------------------


async def _withdraw(assignment_id: int, name: str, slot: str) -> None:
    """A manager takes somebody off a slot. The volunteer taking themselves
    off goes through _self_removal_dialog, whose reason box is its question;
    this is the leader's side, and it asks the plain way."""
    if not await confirm(
        f"Remove {name} from the {slot} slot?",
        detail="Nobody is emailed. Tell them yourself.",
        yes=f"Remove {name} from {slot}",
        danger=True,
    ):
        return

    async def command(ctx: PageCtx):
        return await event_service.remove_assignment(
            ctx.session, ctx.actor, assignment_id, now=ctx.now
        )

    await run_command(command, reload=True, success=f"{name} is off the slot")


async def _assign(slot_id: int, volunteer_id: int | None) -> None:
    if not volunteer_id:
        warn("Pick a person first")
        return

    async def command(ctx: PageCtx):
        return await event_service.assign(
            ctx.session,
            ctx.actor,
            slot_id=slot_id,
            volunteer_id=volunteer_id,
            assigned_by=ctx.actor.account.id,
            now=ctx.now,
        )

    await run_command(command, reload=True, success="Scheduled")


async def _delete_slot(slot_id: int, name: str) -> None:
    if not await confirm(
        f"Delete the slot {name}?",
        detail="It is empty, so nobody loses a place.",
        yes="Delete the slot",
        danger=True,
    ):
        return

    async def command(ctx: PageCtx):
        return await event_service.delete_slot(
            ctx.session, ctx.actor, slot_id, now=ctx.now
        )

    await run_command(command, reload=True, success=f"Deleted the slot {name}")


async def _cancel_event(event_id: int) -> None:
    """Confirm, then cancel. The policy mails everyone signed up, after the
    commit — unless the event was already over, when nobody needs mail
    about it."""
    if not await confirm(
        "Cancel this event? Everyone signed up is emailed, and open "
        "substitute requests are closed with it.",
        yes="Cancel the event",
        no="Keep it",
        danger=True,
    ):
        return

    async def command(ctx: PageCtx):
        return await event_service.cancel_event(
            ctx.session,
            ctx.actor,
            event_id,
            cancelled_by=ctx.actor.account.id,
            now=ctx.now,
        )

    await run_command(command, success="Event cancelled")


async def _add_collaborator(event_id: int, team_id: int | None, label: str) -> None:
    """Add another team's roster to this event, after a word about what
    that creates."""
    if not team_id:
        warn("Pick a team first")
        return
    if not await confirm(
        f"Add {label} to this event?",
        detail=(
            "A temporary task-force team is created holding both rosters: "
            "members of the added team can sign up for slots, its leaders "
            "co-manage the event, and the team is removed automatically "
            "after the event ends (it stays visible in history)."
        ),
        yes="Add team",
        icon="group_add",
        yes_marker="confirm-collaborator",
    ):
        return

    async def command(ctx: PageCtx):
        return await task_force_service.add_collaborating_team(
            ctx.session,
            ctx.actor,
            event_id=event_id,
            source_team_id=team_id,
            created_by=ctx.actor.account.id,
            now=ctx.now,
            tz=ctx.env.tz,
        )

    await run_command(command, success="Team added — their roster can sign up now")


async def _sync_rosters(event_id: int) -> None:
    async def command(ctx: PageCtx):
        return await task_force_service.refresh_rosters(
            ctx.session, ctx.actor, event_id
        )

    def done(added, _effects, _report) -> None:
        flash(f"Rosters synced — {added} member(s) added")

    await run_command(command, on_ok=done, reload=True)


async def _set_rsvp(event_id: int, available: bool, note: str) -> None:
    async def command(ctx: PageCtx):
        return await event_service.set_rsvp(
            ctx.session,
            ctx.actor,
            event_id=event_id,
            volunteer_id=ctx.actor.volunteer_id,
            available=available,
            note=note,
            now=ctx.now,
        )

    await run_command(command, reload=True, success="Answer saved")


async def _save_attendance(assignment_id: int, attended: bool, hours_value) -> None:
    try:
        hours = Decimal(str(hours_value)) if hours_value is not None else None
    except InvalidOperation:
        warn("Hours must be a number")
        return

    async def command(ctx: PageCtx):
        return await event_service.set_attendance(
            ctx.session,
            ctx.actor,
            assignment_id=assignment_id,
            attended=attended,
            hours=hours,
            now=ctx.now,
        )

    await run_command(command, reload=True, success="Attendance saved")


async def _clear_attendance(assignment_id: int) -> None:
    """Back to the automatic answer."""

    async def command(ctx: PageCtx):
        return await event_service.set_attendance(
            ctx.session,
            ctx.actor,
            assignment_id=assignment_id,
            attended=None,
            hours=None,
            now=ctx.now,
        )

    await run_command(command, reload=True, success="Back to the automatic answer")


# --- the workroom's sections ---------------------------------------------------
#
# One function per card or list on /events/{id}, in the order the page draws
# them. NiceGUI's slot stack is dynamic, so a section called inside
# `with frame(...)` adds to that frame like inline code would.


def _event_header(
    room: EventWorkroom, base_url: str, *, now: datetime, tz: ZoneInfo
) -> None:
    """Team, status, when and where; Share for everyone, Edit and Cancel for
    a manager while the event is still scheduled."""
    event = room.event
    with ui.row().classes("w-full items-center gap-2"):
        ui.link(room.view.path, f"/teams/{event.team_id}").classes("font-medium")
        _status_badge(event, now=now, tz=tz)
        ui.label(timefmt.event_when(event.starts_at, event.ends_at, tz=tz)).classes(
            "text-sm text-gray-600"
        )
        if event.location:
            ui.label(f"· {event.location}").classes("text-sm text-gray-600")
        ui.space()
        _share_panel(base_url, event.id)
        if room.can_manage and event.status == EventStatus.scheduled.value:
            ui.button(
                "Edit", icon="edit", on_click=lambda: _edit_event_dialog(event)
            ).props("dense outline")
            ui.button(
                "Cancel event",
                on_click=lambda: _cancel_event(event.id),
            ).props("dense outline color=negative")
    if event.description:
        ui.label(event.description).classes("text-sm text-gray-600")


def _collaboration_card(
    event_id: int,
    tf_view: task_force_service.TaskForceView | None,
    source_paths: list[str],
    collaborator_options: dict[int, str],
) -> None:
    """Add another team's roster to this event, or re-copy the ones already in.

    Manager-only, upcoming events only — the caller gates that."""
    with ui.card().classes("w-full gap-2 p-3"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("Collaboration").classes("font-medium")
            if tf_view is not None:
                ui.badge("task force", color="secondary")
            ui.space()
            if tf_view is not None:
                ui.button(
                    "Sync rosters",
                    icon="sync",
                    on_click=lambda: _sync_rosters(event_id),
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
                "Need another ministry for this event? After asking their "
                "ministry team leader for permission, adding a "
                "collaborating team creates a temporary task-force "
                "team holding both rosters, so everyone can sign up; "
                "it is removed automatically after the event."
            ).classes("text-sm text-gray-500")
        if collaborator_options:
            with ui.row().classes("w-full items-center gap-2"):
                pick = (
                    ui.select(
                        collaborator_options,
                        label="Add collaborating team",
                        with_input=True,
                    )
                    .props("outlined dense")
                    .classes("w-72")
                )
                ui.button(
                    "Add",
                    icon="group_add",
                    on_click=lambda: _add_collaborator(
                        event_id,
                        pick.value,
                        collaborator_options.get(pick.value, "that team"),
                    ),
                ).props("dense outline").mark("add-collaborator")


def _availability_card(event_id: int, my_rsvp: EventRsvp | None) -> None:
    """ "Can you serve?" — an answer, not a commitment; the assignment is that."""
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
                on_click=lambda: _set_rsvp(event_id, True, note.value),
            ).props("dense outline color=positive")
            ui.button(
                "Not available",
                icon="thumb_down",
                on_click=lambda: _set_rsvp(event_id, False, note.value),
            ).props("dense outline")


def _assignment_row(
    room: EventWorkroom,
    assignment: EventAssignment,
    volunteer: Volunteer,
    panel: VolunteerPanel,
    options: dict[int, str],
    *,
    slot_name: str,
) -> None:
    """One person on a slot: their name and badges, then the controls --
    the assignee's own (a substitute call, a hand-off, withdrawing) or the
    manager's Remove."""
    mine = volunteer.id == room.my_volunteer_id
    with ui.row().classes("w-full items-center gap-2 p-1 rounded hover:bg-gray-100"):
        volunteer_link(volunteer.full_name, volunteer.id, panel)
        if assignment.kind == "sub":
            ui.badge("substitute", color="secondary")
        rsvp = room.rsvp_by_volunteer.get(volunteer.id)
        if rsvp is not None and not rsvp.available:
            ui.badge("marked unavailable", color="warning")
        if assignment.id in room.sub_wanted:
            ui.badge("sub wanted", color="warning")
        ui.space()
        if room.upcoming and mine and assignment.id not in room.sub_wanted:
            ui.button(
                "Need a sub",
                icon="campaign",
                on_click=lambda _, aid=assignment.id: _sub_request_dialog(aid),
            ).props("dense outline")
        if room.upcoming and mine:
            # handing off with an open sub call cancels the call
            ui.button(
                "Hand off",
                icon="swap_horiz",
                on_click=lambda _, aid=assignment.id: _substitute_dialog(aid, options),
            ).props("dense outline")
            ui.button(
                "Withdraw",
                on_click=lambda _, aid=assignment.id: _self_removal_dialog(aid),
            ).props("dense flat")
        elif room.upcoming and room.can_manage:
            ui.button(
                "Remove",
                on_click=lambda _, aid=assignment.id, who=volunteer.full_name: (
                    _withdraw(aid, who, slot_name)
                ),
            ).props("dense flat").mark(f"remove-assignment-{assignment.id}")


def _slot_card(
    room: EventWorkroom,
    sv: event_service.SlotView,
    panel: VolunteerPanel,
    options: dict[int, str],
) -> None:
    """One slot: its name and fill, the sign-up button for a member with no
    slot yet, the manager's edit and delete, everyone on it, and for a
    manager the picker that schedules somebody."""
    slot = sv.slot
    has_room = sv.open_spots is None or sv.open_spots > 0
    with ui.card().classes("w-full gap-2 p-3"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(slot.name).classes("font-medium")
            cap = "∞" if slot.capacity is None else str(slot.capacity)
            ui.badge(f"{len(sv.entries)}/{cap}")
            ui.space()
            if (
                room.am_member
                and room.upcoming
                and room.my_assignment is None
                and has_room
            ):
                ui.button(
                    "Sign up",
                    icon="person_add",
                    on_click=lambda _, sid=slot.id, sn=slot.name: _signup_dialog(
                        sid, sn, series=room.series
                    ),
                ).props("dense outline")
            if room.can_manage and room.upcoming:
                ui.button(
                    icon="edit", on_click=lambda _, s=slot: _edit_slot_dialog(s)
                ).props("dense flat").mark(f"slot-edit-{slot.id}").tooltip(
                    "Rename this slot, change how many it holds, or "
                    "reword its description"
                )
            if room.can_manage and room.upcoming and not sv.entries:
                ui.button(
                    icon="delete",
                    on_click=lambda _, sid=slot.id, sn=slot.name: _delete_slot(sid, sn),
                ).props("dense flat").mark(f"slot-delete-{slot.id}").tooltip(
                    "Remove this empty slot"
                )
        if slot.description:
            ui.label(slot.description).classes("text-sm text-gray-600")
        for assignment, volunteer in sv.entries:
            _assignment_row(
                room, assignment, volunteer, panel, options, slot_name=slot.name
            )
        if room.can_manage and room.upcoming and options and has_room:
            with ui.row().classes("w-full items-center gap-2"):
                pick = (
                    ui.select(options, label="Schedule someone", with_input=True)
                    .props("outlined dense")
                    .classes("w-64")
                )
                ui.button(
                    "Assign",
                    on_click=lambda _, sid=slot.id, p=pick: _assign(sid, p.value),
                ).props("dense outline")


def _slots_section(room: EventWorkroom, panel: VolunteerPanel) -> None:
    """The slot list, and for a manager the button that adds one."""
    ui.label("Slots").classes("text-lg font-medium mt-2")
    options = room.picker_options()
    for sv in room.view.slots:
        _slot_card(room, sv, panel, options)
    if room.can_manage and room.upcoming:
        ui.button(
            "Add slot", icon="add", on_click=lambda: _add_slot_dialog(room.event.id)
        ).props("dense flat no-caps")


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


@ui.page("/events/{event_id}")
async def event_detail_page(event_id: int):
    async with page_ctx() as ctx:
        actor, tz = ctx.actor, ctx.env.tz
        shown = await readmodels.event_workroom(
            ctx.session, actor, event_id, now=ctx.now
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
    room = shown.value

    panel = VolunteerPanel("", ctx.base_url)
    with frame(room.event.title, actor):
        _event_header(room, ctx.base_url, now=ctx.now, tz=tz)
        if room.can_manage and room.upcoming:
            _collaboration_card(
                event_id, room.task_force, room.source_paths, room.collaborator_options
            )
        if room.am_member and room.upcoming:
            _availability_card(event_id, room.my_rsvp)
        _slots_section(room, panel)
        if room.can_manage and room.view.rsvps:
            _availability_answers(room.view.rsvps, panel)
        if room.claimable_subs:
            _subs_wanted_section(room.claimable_subs, room.view.slots)
        if room.attendance is not None:
            _attendance_section(room.event, room.attendance, panel)
