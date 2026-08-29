"""Scheduling: events, slots, RSVPs, substitutions, attendance.

An event always belongs to exactly one team; membership of that team is the
domain gate for RSVPs, sign-ups, assignments and substitution claims (a
parish-wide occasion gets its own task-force team first). Like every
service, *permission* checks (who may manage, who may view) live here, as
gates that return the refusal -- but team membership is enforced as a domain
invariant, the way elections checks a candidate belongs to its proposal.

Every function that can refuse returns a Result. The clock never runs here:
`now` is the moment the edge read once, `tz` the parish zone it holds, and a
series id is minted where randomness lives. Roster mutations freeze once an
event ended -- against that same `now` -- and are refused on cancelled
events.

Attendance is derived, not entered: an assignment on a past, non-cancelled
event counts as attended for the scheduled duration unless a manager
recorded attended_override / hours_override (see effective()).

sign_up/assign take the slot row FOR UPDATE before counting occupants — the
first use of a row lock in this codebase, because a *counted* capacity has
no partial-unique-index backstop the way one-open-per-seat rules do.
"""

import difflib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain import (
    EventCancelled,
    NotifyMode,
    Outcome,
    SelfRemoved,
    SlotHandedOver,
    SubClaimed,
    SubRequested,
)
from ..errors import (
    DomainError,
    Forbidden,
    Invalid,
    NotFound,
    invalid,
    not_found,
    require,
)
from ..fp import Err, Ok, Result
from ..models import (
    AssignmentKind,
    Event,
    EventAssignment,
    EventRsvp,
    EventSlot,
    EventStatus,
    EventSubRequest,
    Membership,
    Notification,
    NotificationStage,
    SubRequestStatus,
    Team,
    Volunteer,
)
from ..permissions import Actor, volunteer_team_ids
from . import teams as team_service

_UNSET: object = object()

# copy-forward bound: one year of weekly occurrences is the most a single
# create should materialize (concrete rows, no recurrence engine)
MAX_REPEAT_DAYS = 366

# mirrors EventSlot.description. Checked here rather than left to the column:
# the toast translator turns an Invalid into a warning but lets DataError
# escape, so an over-long paste in the GUI would otherwise die unhandled.
SLOT_DESCRIPTION_MAX = 300

TWO_PLACES = Decimal("0.01")


# --- view models --------------------------------------------------------------


@dataclass(frozen=True)
class SlotInput:
    name: str
    capacity: int | None = None  # None = unlimited
    position: int = 0
    description: str | None = None  # a line under the name; never identity


@dataclass(frozen=True)
class EventSummary:
    event: Event
    path: str  # team path, e.g. "Liturgy / Lectors"
    filled: int
    capacity: int | None  # None = at least one unlimited slot
    my_assignment: EventAssignment | None
    my_rsvp: EventRsvp | None


@dataclass(frozen=True)
class SlotView:
    slot: EventSlot
    entries: list[tuple[EventAssignment, Volunteer]]
    open_spots: int | None  # None = unlimited


@dataclass(frozen=True)
class EventDetail:
    event: Event
    path: str
    slots: list[SlotView]
    rsvps: list[tuple[EventRsvp, Volunteer]]
    open_subs: list[tuple[EventSubRequest, EventAssignment]]


@dataclass(frozen=True)
class MyDuty:
    assignment: EventAssignment
    event: Event
    slot: EventSlot
    open_sub: EventSubRequest | None


@dataclass(frozen=True)
class ClaimableSub:
    sub: EventSubRequest
    assignment: EventAssignment
    event: Event
    slot: EventSlot
    volunteer: Volunteer  # the person asking for a substitute
    path: str


@dataclass(frozen=True)
class HoursSummary:
    total_hours: Decimal
    events_attended: int


# --- pure helpers -------------------------------------------------------------


def scheduled_hours(event: Event) -> Decimal:
    """The event's planned duration in hours, to two places."""
    seconds = (event.ends_at - event.starts_at).total_seconds()
    return (Decimal(int(seconds)) / 3600).quantize(TWO_PLACES)


def effective(assignment: EventAssignment, event: Event) -> tuple[bool, Decimal]:
    """(attended, hours) for a past, non-cancelled event's assignment:
    attended unless overridden, for the scheduled duration unless overridden."""
    attended = (
        assignment.attended_override
        if assignment.attended_override is not None
        else True
    )
    if not attended:
        return False, Decimal("0.00")
    if assignment.hours_override is not None:
        return True, assignment.hours_override
    return True, scheduled_hours(event)


def visible_team_ids(actor: Actor) -> set[int] | None:
    """Teams whose events the actor may list: exactly the roster-names domain
    (event pages show assignee names). None = no filter (admin)."""
    if actor.is_admin:
        return None
    return actor.managed_team_ids | actor.full_view_team_ids | actor.names_view_team_ids


def is_past(event: Event, now: datetime) -> bool:
    return event.ends_at <= now


def _require_open(event: Event, action: str, now: datetime) -> Err[Invalid] | None:
    """Roster mutations freeze once the event ended (attendance overrides are
    the only post-event lever) and are refused on cancelled events."""
    if event.status != EventStatus.scheduled.value:
        return invalid(f"cannot {action}: event is cancelled")
    if is_past(event, now):
        return invalid(f"cannot {action}: event has already ended")
    return None


# --- events -------------------------------------------------------------------


def _check_times(starts_at: datetime, ends_at: datetime) -> Err[Invalid] | None:
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        return invalid("event times must carry a timezone")
    if starts_at >= ends_at:
        return invalid("the event must end after it starts")
    return None


def _check_slots(slots: list[SlotInput]) -> Err[Invalid] | None:
    names = [s.name.strip() for s in slots]
    if any(not n for n in names):
        return invalid("slot names cannot be empty")
    if len(set(names)) != len(names):
        return invalid("slot names must be unique within the event")
    if any(s.capacity is not None and s.capacity < 1 for s in slots):
        return invalid("slot capacity must be at least 1 (or blank for unlimited)")
    if any(len((s.description or "").strip()) > SLOT_DESCRIPTION_MAX for s in slots):
        return invalid(
            f"a slot description is at most {SLOT_DESCRIPTION_MAX} characters"
        )
    return None


def _occurrences(
    starts_at: datetime, ends_at: datetime, until: date | None, tz: ZoneInfo
) -> Result[list[tuple[datetime, datetime]], Invalid]:
    """Weekly copy-forward. Wall-clock times repeat in the parish's timezone:
    the dates advance by 7 days and recombine with the local clock time, so a
    10:30 Mass stays 10:30 across a DST change (never timedelta(weeks=1) on
    the instant, which would shift it an hour)."""
    if until is None:
        return Ok([(starts_at, ends_at)])
    local_start = starts_at.astimezone(tz)
    local_end = ends_at.astimezone(tz)
    if until < local_start.date():
        return invalid("the repeat-until date is before the first event")
    if until > local_start.date() + timedelta(days=MAX_REPEAT_DAYS):
        return invalid("repeats are limited to one year of events")
    span_days = (local_end.date() - local_start.date()).days
    out: list[tuple[datetime, datetime]] = []
    day = local_start.date()
    while day <= until:
        s = datetime.combine(day, local_start.time(), tz)
        e = datetime.combine(day + timedelta(days=span_days), local_end.time(), tz)
        out.append((s, e))
        day += timedelta(days=7)
    return Ok(out)


async def create_event(
    session: AsyncSession,
    actor: Actor | None,
    *,
    team_id: int,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    description: str | None = None,
    location: str | None = None,
    slots: list[SlotInput] | None = None,
    repeat_weekly_until: date | None = None,
    created_by: int | None,
    tz: ZoneInfo,
    series_id: UUID | None = None,
) -> Result[list[Event], DomainError]:
    """Create one event — or, with repeat_weekly_until, one per week through
    that date (inclusive), each with its own copy of the slots. No slots
    given means one unlimited "Volunteers" slot. `series_id` is the id the
    edge minted for a weekly series; it is required exactly when
    repeat_weekly_until is given."""
    if denied := require(
        actor is None or actor.can_manage_team(team_id),
        "manage this team's events",
    ):
        return denied
    title = title.strip()
    if not title:
        return invalid("a title is required")
    if bad := _check_times(starts_at, ends_at):
        return bad
    if await session.get(Team, team_id) is None:
        return not_found("team", team_id)
    slot_inputs = slots or [SlotInput("Volunteers")]
    if bad := _check_slots(slot_inputs):
        return bad
    occurrences = _occurrences(starts_at, ends_at, repeat_weekly_until, tz)
    if isinstance(occurrences, Err):
        return occurrences

    # weekly repeats share a series id (even a range that yields one row —
    # it was created AS a series); standalone events carry NULL
    if repeat_weekly_until is not None:
        assert series_id is not None, "a weekly series needs the id the edge minted"
    else:
        series_id = None
    events: list[Event] = []
    for s, e in occurrences.value:
        event = Event(
            team_id=team_id,
            title=title,
            description=description or None,
            location=(location or "").strip() or None,
            starts_at=s,
            ends_at=e,
            created_by=created_by,
            series_id=series_id,
        )
        session.add(event)
        events.append(event)
    await session.flush()  # assigns event ids for the slot rows
    for event in events:
        for slot in slot_inputs:
            session.add(
                EventSlot(
                    event_id=event.id,
                    name=slot.name.strip(),
                    capacity=slot.capacity,
                    position=slot.position,
                    description=_clean_description(slot.description),
                )
            )
    await session.flush()
    return Ok(events)


async def update_event(
    session: AsyncSession,
    actor: Actor | None,
    event_id: int,
    *,
    title: str | object = _UNSET,
    description: str | None | object = _UNSET,
    location: str | None | object = _UNSET,
    starts_at: datetime | object = _UNSET,
    ends_at: datetime | object = _UNSET,
) -> Result[Event, DomainError]:
    """Edit details/times. Allowed on past events (a manager correcting a
    wrong end time legitimately recomputes the auto hours) but not on
    cancelled ones."""
    managed = await _managed(session, actor, event_id)
    if isinstance(managed, Err):
        return managed
    event = managed.value
    if event.status != EventStatus.scheduled.value:
        return invalid("cannot edit: event is cancelled")
    if title is not _UNSET:
        if not str(title).strip():
            return invalid("a title is required")
        event.title = str(title).strip()
    if description is not _UNSET:
        event.description = description or None  # type: ignore[assignment]
    if location is not _UNSET:
        event.location = (str(location or "")).strip() or None
    new_start = event.starts_at if starts_at is _UNSET else starts_at
    new_end = event.ends_at if ends_at is _UNSET else ends_at
    if starts_at is not _UNSET or ends_at is not _UNSET:
        if bad := _check_times(new_start, new_end):  # type: ignore[arg-type]
            return bad
        event.starts_at = new_start  # type: ignore[assignment]
        event.ends_at = new_end  # type: ignore[assignment]
    await session.flush()
    return Ok(event)


async def cancel_event(
    session: AsyncSession,
    actor: Actor | None,
    event_id: int,
    *,
    cancelled_by: int | None,
    now: datetime,
) -> Result[Outcome[Event], DomainError]:
    """Cancel the event and resolve its open substitution requests in the
    same transaction. The EventCancelled event carries the assignees'
    addresses; the policy mails them AFTER the transaction commits (mail
    never rides a transaction), and not at all for an event already over."""
    managed = await _managed(session, actor, event_id)
    if isinstance(managed, Err):
        return managed
    event = managed.value
    if event.status != EventStatus.scheduled.value:
        return invalid("event is already cancelled")
    event.status = EventStatus.cancelled.value
    event.cancelled_at = now
    event.cancelled_by = cancelled_by
    open_subs = sa.select(EventSubRequest.id).where(
        EventSubRequest.status == SubRequestStatus.open.value,
        EventSubRequest.assignment_id.in_(
            sa.select(EventAssignment.id).where(EventAssignment.event_id == event_id)
        ),
    )
    await session.execute(
        sa.update(EventSubRequest)
        .where(EventSubRequest.id.in_(open_subs))
        .values(status=SubRequestStatus.cancelled.value, resolved_at=now)
    )
    emails = list(
        await session.scalars(
            sa.select(sa.distinct(Volunteer.email))
            .join(EventAssignment, EventAssignment.volunteer_id == Volunteer.id)
            .where(EventAssignment.event_id == event_id, Volunteer.email.is_not(None))
        )
    )
    await session.flush()
    paths = (await team_service.tree(session)).paths
    return Ok(
        Outcome(
            event,
            (
                EventCancelled(
                    event_id=event.id,
                    title=event.title,
                    path=paths.get(event.team_id, ""),
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    emails=tuple(sorted(emails)),
                ),
            ),
        )
    )


async def get(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id)


async def _get(session: AsyncSession, event_id: int) -> Result[Event, NotFound]:
    event = await session.get(Event, event_id)
    if event is None:
        return not_found("event", event_id)
    return Ok(event)


# --- slots --------------------------------------------------------------------


async def _managed(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> Result[Event, DomainError]:
    """The event, for a caller who runs its team.

    Managing an event — its details, slots, cancellation, other people's
    assignments, attendance — is can_manage_team on the owning team. For a
    task-force event that is the meta team, which permissions.Actor keeps in
    the managing scope precisely so collaboration works.
    """
    found = await _get(session, event_id)
    if isinstance(found, Err):
        return found
    if denied := require(
        actor is None or actor.can_manage_team(found.value.team_id),
        "manage this team's events",
    ):
        return denied
    return found


async def _visible(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> Result[Event, DomainError]:
    """The event, for a caller who may see the owning team's roster names —
    the documented visibility domain for an event and its assignee list.

    Taking PART is stricter and separate: _require_member enforces real
    membership of the team as a domain invariant, admin or not.
    """
    found = await _get(session, event_id)
    if isinstance(found, Err):
        return found
    if denied := require(
        actor is None or actor.can_view_roster_names(found.value.team_id),
        "view this team's events",
    ):
        return denied
    return found


async def visible(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> Result[Event, DomainError]:
    """Authorize that `actor` may see this event and return it — the light gate
    for endpoints (e.g. the task-force view) that need the event visible but not
    the full detail() load. Keeps the check in the service both surfaces call,
    not at a front door."""
    return await _visible(session, actor, event_id)


def _clean_description(text: str | None) -> str | None:
    """Blank is absent: an untouched input must not store an empty string,
    which would render as a stray line under the slot name."""
    return (text or "").strip() or None


async def add_slot(
    session: AsyncSession,
    actor: Actor | None,
    event_id: int,
    *,
    name: str,
    capacity: int | None = None,
    position: int = 0,
    description: str | None = None,
    now: datetime,
) -> Result[EventSlot, DomainError]:
    managed = await _managed(session, actor, event_id)
    if isinstance(managed, Err):
        return managed
    if closed := _require_open(managed.value, "add a slot", now):
        return closed
    if bad := _check_slots([SlotInput(name, capacity, position, description)]):
        return bad
    existing = await session.scalar(
        sa.select(EventSlot.id).where(
            EventSlot.event_id == event_id, EventSlot.name == name.strip()
        )
    )
    if existing is not None:
        return invalid("a slot with that name already exists")
    slot = EventSlot(
        event_id=event_id,
        name=name.strip(),
        capacity=capacity,
        position=position,
        description=_clean_description(description),
    )
    session.add(slot)
    await session.flush()
    return Ok(slot)


async def update_slot(
    session: AsyncSession,
    actor: Actor | None,
    slot_id: int,
    *,
    name: str | object = _UNSET,
    capacity: int | None | object = _UNSET,
    position: int | object = _UNSET,
    description: str | None | object = _UNSET,
    now: datetime,
) -> Result[EventSlot, DomainError]:
    slot = await session.get(EventSlot, slot_id)
    if slot is None:
        return not_found("slot", slot_id)
    managed = await _managed(session, actor, slot.event_id)
    if isinstance(managed, Err):
        return managed
    if closed := _require_open(managed.value, "edit a slot", now):
        return closed
    if name is not _UNSET:
        if not str(name).strip():
            return invalid("slot names cannot be empty")
        slot.name = str(name).strip()
    if capacity is not _UNSET:
        if capacity is not None and int(capacity) < 1:  # type: ignore[arg-type]
            return invalid("slot capacity must be at least 1 (or blank for unlimited)")
        if capacity is not None:
            occupied = await session.scalar(
                sa.select(sa.func.count()).where(EventAssignment.slot_id == slot.id)
            )
            if occupied is not None and occupied > int(capacity):  # type: ignore[arg-type]
                return invalid(f"{occupied} people already fill this slot")
        slot.capacity = capacity  # type: ignore[assignment]
    if position is not _UNSET:
        slot.position = int(position)  # type: ignore[arg-type]
    if description is not _UNSET:
        text = _clean_description(description)  # type: ignore[arg-type]
        if text is not None and len(text) > SLOT_DESCRIPTION_MAX:
            return invalid(
                f"a slot description is at most {SLOT_DESCRIPTION_MAX} characters"
            )
        slot.description = text
    await session.flush()
    return Ok(slot)


async def delete_slot(
    session: AsyncSession, actor: Actor | None, slot_id: int, *, now: datetime
) -> Result[Event, DomainError]:
    slot = await session.get(EventSlot, slot_id)
    if slot is None:
        return not_found("slot", slot_id)
    managed = await _managed(session, actor, slot.event_id)
    if isinstance(managed, Err):
        return managed
    event = managed.value
    if closed := _require_open(event, "remove a slot", now):
        return closed
    occupied = await session.scalar(
        sa.select(sa.func.count()).where(EventAssignment.slot_id == slot.id)
    )
    if occupied:
        return invalid("remove its people first — the slot is not empty")
    remaining = await session.scalar(
        sa.select(sa.func.count()).where(EventSlot.event_id == event.id)
    )
    if remaining is not None and remaining <= 1:
        return invalid("an event needs at least one slot")
    await session.delete(slot)
    await session.flush()
    return Ok(event)


# --- listings -----------------------------------------------------------------


async def list_events(
    session: AsyncSession,
    actor: Actor,
    *,
    team_id: int | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    include_cancelled: bool = False,
) -> list[EventSummary]:
    """Events the actor may see (visible_team_ids scope), oldest first.
    from_/to bound starts_at."""
    visible = visible_team_ids(actor)
    if visible is not None and not visible and team_id is None:
        return []
    stmt = sa.select(Event).order_by(Event.starts_at, Event.id)
    if visible is not None:
        stmt = stmt.where(Event.team_id.in_(visible))
    if team_id is not None:
        stmt = stmt.where(Event.team_id == team_id)
    if from_ is not None:
        stmt = stmt.where(Event.starts_at >= from_)
    if to is not None:
        stmt = stmt.where(Event.starts_at < to)
    if not include_cancelled:
        stmt = stmt.where(Event.status == EventStatus.scheduled.value)
    events = list(await session.scalars(stmt))
    if not events:
        return []
    event_ids = [e.id for e in events]
    paths = (await team_service.tree(session)).paths

    slot_rows = (
        await session.execute(
            sa.select(EventSlot.event_id, EventSlot.capacity).where(
                EventSlot.event_id.in_(event_ids)
            )
        )
    ).all()
    capacity: dict[int, int | None] = {}
    for eid, cap in slot_rows:
        if eid in capacity and capacity[eid] is None:
            continue
        if cap is None:
            capacity[eid] = None  # one unlimited slot makes the event unlimited
        else:
            capacity[eid] = (capacity.get(eid) or 0) + cap

    filled = dict(
        (
            await session.execute(
                sa.select(EventAssignment.event_id, sa.func.count())
                .where(EventAssignment.event_id.in_(event_ids))
                .group_by(EventAssignment.event_id)
            )
        ).all()
    )

    mine: dict[int, EventAssignment] = {}
    my_rsvps: dict[int, EventRsvp] = {}
    if actor.volunteer_id is not None:
        mine = {
            a.event_id: a
            for a in await session.scalars(
                sa.select(EventAssignment).where(
                    EventAssignment.event_id.in_(event_ids),
                    EventAssignment.volunteer_id == actor.volunteer_id,
                )
            )
        }
        my_rsvps = {
            r.event_id: r
            for r in await session.scalars(
                sa.select(EventRsvp).where(
                    EventRsvp.event_id.in_(event_ids),
                    EventRsvp.volunteer_id == actor.volunteer_id,
                )
            )
        }

    return [
        EventSummary(
            event=e,
            path=paths.get(e.team_id, f"team {e.team_id}"),
            filled=filled.get(e.id, 0),
            capacity=capacity.get(e.id),
            my_assignment=mine.get(e.id),
            my_rsvp=my_rsvps.get(e.id),
        )
        for e in events
    ]


@dataclass(frozen=True)
class CalendarEntry:
    """One event as a calendar shows it: its public fields, the slot the reader
    holds at it (the "mine" scope), the team path, and whether the reader may
    open it — the parish-wide scope lists every team's events, so a reader
    off a team sees that event's title and time (what the public Google
    calendar shows anyone) without a link into a page that would refuse them."""

    event: Event
    slot_name: str | None
    path: str
    visible: bool


async def calendar_entries(
    session: AsyncSession,
    actor: Actor | None,
    *,
    scope: str,
    from_: datetime,
    to: datetime,
) -> Result[list[CalendarEntry], Invalid]:
    """Scheduled events starting in [from_, to), oldest first.

    scope="mine": the events the reader holds a slot at — the same rows as
    my_upcoming, windowed instead of "not yet over". Nobody signed in, or
    an account with no volunteer, has no duties.
    scope="parish": every team's scheduled events; visibility only decides
    the link. Anonymous callers (the public feed) get no links at all.
    """
    if scope not in ("mine", "parish"):
        return invalid(f"unknown calendar scope {scope!r}")
    window = (
        Event.status == EventStatus.scheduled.value,
        Event.starts_at >= from_,
        Event.starts_at < to,
    )
    paths = (await team_service.tree(session)).paths
    if scope == "mine":
        if actor is None or actor.volunteer_id is None:
            return Ok([])
        rows = (
            await session.execute(
                sa.select(Event, EventSlot.name)
                .join(EventAssignment, EventAssignment.event_id == Event.id)
                .join(EventSlot, EventSlot.id == EventAssignment.slot_id)
                .where(EventAssignment.volunteer_id == actor.volunteer_id, *window)
                .order_by(Event.starts_at, Event.id)
            )
        ).all()
        visible = visible_team_ids(actor)
        return Ok(
            [
                CalendarEntry(
                    event=e,
                    slot_name=slot,
                    path=paths.get(e.team_id, f"team {e.team_id}"),
                    visible=visible is None or e.team_id in visible,
                )
                for e, slot in rows
            ]
        )
    events = await session.scalars(
        sa.select(Event).where(*window).order_by(Event.starts_at, Event.id)
    )
    visible = visible_team_ids(actor) if actor is not None else set()
    return Ok(
        [
            CalendarEntry(
                event=e,
                slot_name=None,
                path=paths.get(e.team_id, f"team {e.team_id}"),
                visible=visible is None or e.team_id in visible,
            )
            for e in events
        ]
    )


# similarity floor for the double-booking warning: "Parish Hall" still hits
# "parish  hall" or "Parish hall (main)", while "Rectory" stays clear of it
SIMILARITY_THRESHOLD = 0.6


@dataclass(frozen=True)
class SimilarEvent:
    starts_at: datetime
    ends_at: datetime
    location: str
    team_path: str
    title: str | None  # None: outside the actor's visible teams — masked


def _norm_location(text: str) -> str:
    return " ".join(text.casefold().split())


async def similar_events(
    session: AsyncSession,
    actor: Actor,
    *,
    starts_at: datetime,
    ends_at: datetime,
    repeat_until: date | None = None,
    location: str | None,
    limit: int = 5,
    tz: ZoneInfo,
) -> Result[list[SimilarEvent], Invalid]:
    """Scheduled events sharing a parish-local day with any occurrence of the
    proposed event, at a location that reads like `location` — the advisory
    double-booking check behind the create dialog's warning.

    Deliberately NOT scoped by visible_team_ids: a collision with a team the
    creator cannot see is exactly the case worth warning about. What that
    leaks is kept small — the when/where and the team path (the team
    directory is readable by every member anyway); the title is masked
    outside the actor's scope. difflib does the matching: parish-scale event
    counts need no index (pg_trgm's similarity() is there if this ever grows).
    """
    # Naive input would be read as the server's clock (container UTC), not the
    # parish's, shifting the calendar day and the whole day-overlap window. The
    # GUI always sends aware datetimes; the JSON API surface must too, the same
    # rule create enforces via _check_times.
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        return invalid("event times must carry a timezone")
    wanted = _norm_location(location or "")
    if not wanted:
        return Ok([])
    occurrences = _occurrences(starts_at, ends_at, repeat_until, tz)
    if isinstance(occurrences, Err):
        return occurrences
    day_set: set[date] = set()
    for occ_start, occ_end in occurrences.value:
        day = occ_start.astimezone(tz).date()
        last = occ_end.astimezone(tz).date()
        while day <= last:
            day_set.add(day)
            day += timedelta(days=1)
    window_start = datetime.combine(min(day_set), time.min, tz)
    window_end = datetime.combine(max(day_set) + timedelta(days=1), time.min, tz)
    candidates = await session.scalars(
        sa.select(Event)
        .where(
            Event.status == EventStatus.scheduled.value,
            Event.starts_at < window_end,
            Event.ends_at > window_start,
            Event.location.is_not(None),
        )
        .order_by(Event.starts_at, Event.id)
    )
    visible = visible_team_ids(actor)
    paths = (await team_service.tree(session)).paths
    out: list[SimilarEvent] = []
    for event in candidates:
        have = _norm_location(event.location or "")
        if not have:
            continue
        if difflib.SequenceMatcher(None, wanted, have).ratio() < SIMILARITY_THRESHOLD:
            continue
        first = event.starts_at.astimezone(tz).date()
        last = event.ends_at.astimezone(tz).date()
        if not any(first <= day <= last for day in day_set):
            continue  # inside the window but not on a shared calendar day
        out.append(
            SimilarEvent(
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                location=event.location or "",
                team_path=paths.get(event.team_id, f"team {event.team_id}"),
                title=(
                    event.title if visible is None or event.team_id in visible else None
                ),
            )
        )
        if len(out) >= limit:
            break
    return Ok(out)


async def detail(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> Result[EventDetail, DomainError]:
    """One event with its slots, entries, RSVPs and open substitution calls.

    Roster-names rights on the owning team, which is the documented visibility
    domain for an event and the names of who is staffing it."""
    shown = await _visible(session, actor, event_id)
    if isinstance(shown, Err):
        return shown
    event = shown.value
    paths = (await team_service.tree(session)).paths
    slots = list(
        await session.scalars(
            sa.select(EventSlot)
            .where(EventSlot.event_id == event_id)
            .order_by(EventSlot.position, EventSlot.id)
        )
    )
    entry_rows = (
        await session.execute(
            sa.select(EventAssignment, Volunteer)
            .join(Volunteer, Volunteer.id == EventAssignment.volunteer_id)
            .where(EventAssignment.event_id == event_id)
            .order_by(Volunteer.last_name, Volunteer.first_name)
        )
    ).all()
    by_slot: dict[int, list[tuple[EventAssignment, Volunteer]]] = {}
    for assignment, volunteer in entry_rows:
        by_slot.setdefault(assignment.slot_id, []).append((assignment, volunteer))
    rsvps = (
        await session.execute(
            sa.select(EventRsvp, Volunteer)
            .join(Volunteer, Volunteer.id == EventRsvp.volunteer_id)
            .where(EventRsvp.event_id == event_id)
            .order_by(Volunteer.last_name, Volunteer.first_name)
        )
    ).all()
    open_subs = (
        await session.execute(
            sa.select(EventSubRequest, EventAssignment)
            .join(
                EventAssignment,
                EventAssignment.id == EventSubRequest.assignment_id,
            )
            .where(
                EventAssignment.event_id == event_id,
                EventSubRequest.status == SubRequestStatus.open.value,
            )
        )
    ).all()
    return Ok(
        EventDetail(
            event=event,
            path=paths.get(event.team_id, f"team {event.team_id}"),
            slots=[
                SlotView(
                    slot=s,
                    entries=by_slot.get(s.id, []),
                    open_spots=(
                        None
                        if s.capacity is None
                        else max(s.capacity - len(by_slot.get(s.id, [])), 0)
                    ),
                )
                for s in slots
            ],
            rsvps=[(r, v) for r, v in rsvps],
            open_subs=[(sub, a) for sub, a in open_subs],
        )
    )


async def my_upcoming(
    session: AsyncSession, volunteer_id: int, *, now: datetime
) -> list[MyDuty]:
    rows = (
        await session.execute(
            sa.select(EventAssignment, Event, EventSlot)
            .join(Event, Event.id == EventAssignment.event_id)
            .join(EventSlot, EventSlot.id == EventAssignment.slot_id)
            .where(
                EventAssignment.volunteer_id == volunteer_id,
                Event.status == EventStatus.scheduled.value,
                Event.ends_at > now,
            )
            .order_by(Event.starts_at)
        )
    ).all()
    subs = {
        s.assignment_id: s
        for s in await session.scalars(
            sa.select(EventSubRequest).where(
                EventSubRequest.assignment_id.in_([a.id for a, _, _ in rows] or [0]),
                EventSubRequest.status == SubRequestStatus.open.value,
            )
        )
    }
    return [
        MyDuty(assignment=a, event=e, slot=s, open_sub=subs.get(a.id))
        for a, e, s in rows
    ]


async def claimable_subs(
    session: AsyncSession, actor: Actor, *, now: datetime
) -> list[ClaimableSub]:
    """Open substitution requests the actor could claim: upcoming events on
    teams they belong to (a real membership, not a cascaded view right),
    excluding their own requests and events they already serve at."""
    if actor.volunteer_id is None:
        return []
    already_assigned = (
        sa.select(EventAssignment.event_id)
        .where(EventAssignment.volunteer_id == actor.volunteer_id)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            sa.select(EventSubRequest, EventAssignment, Event, EventSlot, Volunteer)
            .join(
                EventAssignment,
                EventAssignment.id == EventSubRequest.assignment_id,
            )
            .join(Event, Event.id == EventAssignment.event_id)
            .join(EventSlot, EventSlot.id == EventAssignment.slot_id)
            .join(Volunteer, Volunteer.id == EventAssignment.volunteer_id)
            .join(
                Membership,
                sa.and_(
                    Membership.team_id == Event.team_id,
                    Membership.volunteer_id == actor.volunteer_id,
                ),
            )
            .where(
                EventSubRequest.status == SubRequestStatus.open.value,
                Event.status == EventStatus.scheduled.value,
                Event.ends_at > now,
                EventAssignment.volunteer_id != actor.volunteer_id,
                Event.id.not_in(already_assigned),
            )
            .order_by(Event.starts_at)
        )
    ).all()
    paths = (await team_service.tree(session)).paths
    return [
        ClaimableSub(
            sub=sub,
            assignment=a,
            event=e,
            slot=s,
            volunteer=v,
            path=paths.get(e.team_id, f"team {e.team_id}"),
        )
        for sub, a, e, s, v in rows
    ]


# --- membership & audiences ---------------------------------------------------


async def is_member(session: AsyncSession, volunteer_id: int, team_id: int) -> bool:
    return (
        await session.scalar(
            sa.select(Membership.id).where(
                Membership.volunteer_id == volunteer_id,
                Membership.team_id == team_id,
            )
        )
        is not None
    )


def _require_own_or_managed(
    actor: Actor | None, assignment: EventAssignment, event: Event, what: str
) -> Err[Forbidden] | None:
    """Your own slot, or a slot on an event you manage.

    Withdrawing, asking for a substitute and cancelling that request are all
    things an assignee does for themselves and a manager may do for anyone on
    their team's event."""
    return require(
        actor is None
        or actor.volunteer_id == assignment.volunteer_id
        or actor.can_manage_team(event.team_id),
        what,
    )


def _require_self(
    actor: Actor | None, volunteer_id: int | None, what: str
) -> Err[Forbidden] | None:
    """Taking part is something you do for yourself.

    `volunteer_id` reaches these functions from the request, so it is checked
    against the actor rather than trusted: nobody — however privileged — signs
    somebody else up, RSVPs for them, or claims a substitution as them.
    Scheduling another person is a different function (assign), with a
    manager's check on it."""
    return require(
        actor is None
        or (volunteer_id is not None and actor.volunteer_id == volunteer_id),
        what,
    )


async def _require_member(
    session: AsyncSession, volunteer_id: int | None, team_id: int
) -> Result[int, Invalid]:
    """The domain gate on every participation write; the volunteer id."""
    if volunteer_id is None:
        return invalid("your account is not linked to a volunteer record")
    if not await is_member(session, volunteer_id, team_id):
        return invalid("only members of the event's team can take part")
    return Ok(volunteer_id)


async def member_emails(
    session: AsyncSession,
    team_id: int,
    *,
    exclude_volunteer_ids: frozenset[int] | set[int] = frozenset(),
) -> list[str]:
    """Emails of the team's members — the audience for a substitution call."""
    stmt = (
        sa.select(sa.distinct(Volunteer.email))
        .join(Membership, Membership.volunteer_id == Volunteer.id)
        .where(Membership.team_id == team_id, Volunteer.email.is_not(None))
    )
    if exclude_volunteer_ids:
        stmt = stmt.where(Volunteer.id.not_in(exclude_volunteer_ids))
    return sorted(await session.scalars(stmt))


async def assigned_volunteer_ids(session: AsyncSession, event_id: int) -> set[int]:
    return set(
        await session.scalars(
            sa.select(EventAssignment.volunteer_id).where(
                EventAssignment.event_id == event_id
            )
        )
    )


# --- RSVPs --------------------------------------------------------------------


async def set_rsvp(
    session: AsyncSession,
    actor: Actor | None,
    *,
    event_id: int,
    volunteer_id: int | None,
    available: bool,
    note: str | None = None,
    now: datetime,
) -> Result[None, DomainError]:
    """Upsert the volunteer's availability answer (ballot-PUT semantics)."""
    if denied := _require_self(
        actor, volunteer_id, "answer availability for this volunteer"
    ):
        return denied
    found = await _get(session, event_id)
    if isinstance(found, Err):
        return found
    if closed := _require_open(found.value, "answer availability", now):
        return closed
    member = await _require_member(session, volunteer_id, found.value.team_id)
    if isinstance(member, Err):
        return member
    note = (note or "").strip()[:200] or None
    await session.execute(
        pg_insert(EventRsvp)
        .values(
            event_id=event_id, volunteer_id=member.value, available=available, note=note
        )
        .on_conflict_do_update(
            constraint="uq_event_rsvp",
            set_={"available": available, "note": note, "updated_at": now},
        )
    )
    return Ok(None)


# --- assignments --------------------------------------------------------------


async def _mark_notified(
    session: AsyncSession, assignment_id: int, stage: NotificationStage
) -> None:
    """Record a notice as already delivered, so the nightly digest skips it.

    Used where the person has just been told by the very action they took (a
    self sign-up, a claimed substitution) or by a message the caller sends right
    after commit (a hand-off). ON CONFLICT DO NOTHING because the point is
    idempotence, not the row."""
    await session.execute(
        pg_insert(Notification)
        .values(assignment_id=assignment_id, stage=stage)
        .on_conflict_do_nothing(constraint="uq_notification_assignment")
    )


async def _reset_reminders(session: AsyncSession, assignment_id: int) -> None:
    """Forget the reminders sent for this assignment: the slot changed hands, so
    the incoming person has not had them. The "scheduled" notice is handled
    separately by the caller — they are being told directly."""
    await session.execute(
        sa.delete(Notification).where(
            Notification.assignment_id == assignment_id,
            Notification.stage.in_(
                (NotificationStage.event_week, NotificationStage.event_day)
            ),
        )
    )


async def _join_slot(
    session: AsyncSession,
    *,
    slot_id: int,
    volunteer_id: int,
    kind: AssignmentKind,
    assigned_by: int | None,
    now: datetime,
    notify_7d: bool = False,
    notify_24h: bool = True,
) -> Result[EventAssignment, DomainError]:
    # FOR UPDATE: a counted capacity has no unique-index backstop, so the two
    # concurrent sign-ups that both counted a free spot must serialize here
    slot = (
        await session.execute(
            sa.select(EventSlot).where(EventSlot.id == slot_id).with_for_update()
        )
    ).scalar_one_or_none()
    if slot is None:
        return not_found("slot", slot_id)
    found = await _get(session, slot.event_id)
    if isinstance(found, Err):
        return found
    event = found.value
    if closed := _require_open(event, "join this event", now):
        return closed
    member = await _require_member(session, volunteer_id, event.team_id)
    if isinstance(member, Err):
        return member
    existing = await session.scalar(
        sa.select(EventAssignment.id).where(
            EventAssignment.event_id == event.id,
            EventAssignment.volunteer_id == volunteer_id,
        )
    )
    if existing is not None:
        return invalid("they already serve at this event")
    if slot.capacity is not None:
        occupied = await session.scalar(
            sa.select(sa.func.count()).where(EventAssignment.slot_id == slot.id)
        )
        if occupied is not None and occupied >= slot.capacity:
            return invalid(f"{slot.name} is full")
    assignment = EventAssignment(
        slot_id=slot.id,
        event_id=event.id,
        volunteer_id=volunteer_id,
        kind=kind.value,
        assigned_by=assigned_by,
        notify_7d=notify_7d,
        notify_24h=notify_24h,
    )
    session.add(assignment)
    await session.flush()
    if kind is not AssignmentKind.assigned:
        # the person acted themselves, so the digest's "you have been scheduled"
        # notice would be telling them what they just did
        await _mark_notified(session, assignment.id, NotificationStage.event_scheduled)
    return Ok(assignment)


async def sign_up(
    session: AsyncSession,
    actor: Actor | None,
    *,
    slot_id: int,
    volunteer_id: int | None,
    now: datetime,
    notify_7d: bool = False,
    notify_24h: bool = True,
) -> Result[EventAssignment, DomainError]:
    if volunteer_id is None:
        return invalid("your account is not linked to a volunteer record")
    if denied := _require_self(actor, volunteer_id, "sign this volunteer up"):
        return denied
    return await _join_slot(
        session,
        slot_id=slot_id,
        volunteer_id=volunteer_id,
        kind=AssignmentKind.signup,
        assigned_by=None,
        now=now,
        notify_7d=notify_7d,
        notify_24h=notify_24h,
    )


@dataclass(frozen=True)
class SeriesSignupResult:
    """What copying a sign-up forward achieved, for the caller's toast."""

    joined: int  # later weeks joined (beyond the clicked slot)
    skipped_full: int
    skipped_conflict: int  # already serving that week, or its slot is gone


async def sign_up_series(
    session: AsyncSession,
    actor: Actor | None,
    *,
    slot_id: int,
    volunteer_id: int | None,
    now: datetime,
    notify_7d: bool = False,
    notify_24h: bool = True,
) -> Result[tuple[EventAssignment, SeriesSignupResult], DomainError]:
    """Sign up on the given slot, then copy the sign-up onto every later
    week of the same series with a slot of the same NAME (names are the
    series-wide identity — slot ids differ per materialized row). Each week
    joins inside a SAVEPOINT, so a full week or an existing assignment
    skips that week instead of aborting the whole sign-up."""
    signed = await sign_up(
        session,
        actor,
        slot_id=slot_id,
        volunteer_id=volunteer_id,
        now=now,
        notify_7d=notify_7d,
        notify_24h=notify_24h,
    )
    if isinstance(signed, Err):
        return signed
    first = signed.value
    slot = await session.get(EventSlot, first.slot_id)
    event = await session.get(Event, first.event_id)
    assert slot is not None and event is not None  # sign_up just used them
    if event.series_id is None:
        return Ok((first, SeriesSignupResult(0, 0, 0)))
    future_rows = (
        await session.execute(
            sa.select(Event.id, EventSlot.id)
            .join(
                EventSlot,
                sa.and_(EventSlot.event_id == Event.id, EventSlot.name == slot.name),
                isouter=True,
            )
            .where(
                Event.series_id == event.series_id,
                Event.starts_at > event.starts_at,
                Event.status == EventStatus.scheduled.value,
            )
            .order_by(Event.starts_at, Event.id)
        )
    ).all()
    joined = skipped_full = skipped_conflict = 0
    for _future_event_id, future_slot_id in future_rows:
        if future_slot_id is None:
            skipped_conflict += 1  # that week's slot was renamed or removed
            continue
        savepoint = await session.begin_nested()
        week = await _join_slot(
            session,
            slot_id=future_slot_id,
            volunteer_id=first.volunteer_id,
            kind=AssignmentKind.signup,
            assigned_by=None,
            now=now,
            notify_7d=notify_7d,
            notify_24h=notify_24h,
        )
        if isinstance(week, Err):
            await savepoint.rollback()
            if isinstance(week.error, Invalid) and "full" in week.error.message:
                skipped_full += 1
            else:
                skipped_conflict += 1
            continue
        await savepoint.commit()
        joined += 1
    return Ok((first, SeriesSignupResult(joined, skipped_full, skipped_conflict)))


async def assign(
    session: AsyncSession,
    actor: Actor | None,
    *,
    slot_id: int,
    volunteer_id: int,
    assigned_by: int | None,
    now: datetime,
) -> Result[EventAssignment, DomainError]:
    """Schedule somebody else — a manager's act, unlike sign_up."""
    slot = await session.get(EventSlot, slot_id)
    if slot is None:
        return not_found("slot", slot_id)
    managed = await _managed(session, actor, slot.event_id)
    if isinstance(managed, Err):
        return managed
    return await _join_slot(
        session,
        slot_id=slot_id,
        volunteer_id=volunteer_id,
        kind=AssignmentKind.assigned,
        assigned_by=assigned_by,
        now=now,
    )


async def get_assignment(
    session: AsyncSession, assignment_id: int
) -> EventAssignment | None:
    return await session.get(EventAssignment, assignment_id)


async def remove_assignment(
    session: AsyncSession,
    actor: Actor | None,
    assignment_id: int,
    *,
    now: datetime,
    reason: str | None = None,
) -> Result[Outcome[Event], DomainError]:
    """Withdraw/remove a future assignment (its sub requests cascade away).
    Past rosters are frozen — they are the attendance record.

    With a `reason`, this is somebody taking THEMSELVES off a slot and saying
    why: the SelfRemoved event carries the reason and the team's leaders'
    addresses, so the policy can tell them and they can fill the gap."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        return not_found("assignment", assignment_id)
    found = await _get(session, assignment.event_id)
    if isinstance(found, Err):
        return found
    event = found.value
    if denied := _require_own_or_managed(
        actor, assignment, event, "remove this assignment"
    ):
        return denied
    if closed := _require_open(event, "leave this event", now):
        return closed
    events: tuple[SelfRemoved, ...] = ()
    if reason:
        slot = await session.get(EventSlot, assignment.slot_id)
        who = await session.get(Volunteer, assignment.volunteer_id)
        paths = (await team_service.tree(session)).paths
        events = (
            SelfRemoved(
                event_id=event.id,
                title=event.title,
                path=paths.get(event.team_id, ""),
                slot=slot.name if slot else "volunteer",
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                who=who.full_name if who else "A volunteer",
                volunteer_id=assignment.volunteer_id,
                reason=reason,
                leader_emails=tuple(
                    await team_service.leader_emails(session, event.team_id)
                ),
            ),
        )
    await session.delete(assignment)
    await session.flush()
    return Ok(Outcome(event, events))


# --- substitutions ------------------------------------------------------------


async def request_sub(
    session: AsyncSession,
    actor: Actor | None,
    *,
    assignment_id: int,
    requested_by: int | None,
    note: str | None = None,
    now: datetime,
) -> Result[Outcome[EventSubRequest], DomainError]:
    """Open a substitution call. The friendly pre-check keeps repeat clicks
    from re-mailing the team; uq_event_sub_request_open backstops the race.

    The SubRequested event carries what the call needs to be announced: the
    teammates not already serving (gathered here, NOT via the roster-gated
    detail(): an assignee since removed from the team may still hand off
    their own slot), the asker, the slot and the note. Whether it is
    announced -- the team's daily allowance -- is the policy's call."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        return not_found("assignment", assignment_id)
    found = await _get(session, assignment.event_id)
    if isinstance(found, Err):
        return found
    event = found.value
    if denied := _require_own_or_managed(
        actor, assignment, event, "ask for a substitute here"
    ):
        return denied
    if closed := _require_open(event, "ask for a substitute", now):
        return closed
    existing = await session.scalar(
        sa.select(EventSubRequest.id).where(
            EventSubRequest.assignment_id == assignment_id,
            EventSubRequest.status == SubRequestStatus.open.value,
        )
    )
    if existing is not None:
        return invalid("a substitute request is already open for this slot")
    sub = EventSubRequest(
        assignment_id=assignment_id,
        requested_by=requested_by,
        note=(note or "").strip()[:200] or None,
    )
    session.add(sub)
    await session.flush()
    asker = await session.get(Volunteer, assignment.volunteer_id)
    slot = await session.get(EventSlot, assignment.slot_id)
    paths = (await team_service.tree(session)).paths
    audience = await member_emails(
        session,
        event.team_id,
        exclude_volunteer_ids=await assigned_volunteer_ids(session, event.id),
    )
    return Ok(
        Outcome(
            sub,
            (
                SubRequested(
                    team_id=event.team_id,
                    event_id=event.id,
                    title=event.title,
                    path=paths.get(event.team_id, f"team {event.team_id}"),
                    slot=slot.name if slot else "volunteer",
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    asker=asker.full_name if asker else "A teammate",
                    note=sub.note,
                    audience=tuple(audience),
                ),
            ),
        )
    )


async def claim_sub(
    session: AsyncSession,
    actor: Actor | None,
    *,
    sub_request_id: int,
    volunteer_id: int | None,
    now: datetime,
) -> Result[Outcome[tuple[EventSubRequest, EventAssignment, Volunteer]], DomainError]:
    """First-come claim: the guarded UPDATE ... WHERE status='open' decides
    the race (the loser's rowcount is 0), and the assignment moves to the
    claimant in the same transaction — they commit or roll back together
    (the appoint() pattern). The SubClaimed event names the person who
    asked; the policy tells them after commit."""
    sub = await session.get(EventSubRequest, sub_request_id)
    if sub is None:
        return not_found("substitute request", sub_request_id)
    assignment = await session.get(EventAssignment, sub.assignment_id)
    if assignment is None:  # pragma: no cover - CASCADE removes sub with it
        return not_found("assignment")
    found = await _get(session, assignment.event_id)
    if isinstance(found, Err):
        return found
    event = found.value
    if denied := _require_self(
        actor, volunteer_id, "claim this slot for this volunteer"
    ):
        return denied
    if closed := _require_open(event, "claim this slot", now):
        return closed
    member = await _require_member(session, volunteer_id, event.team_id)
    if isinstance(member, Err):
        return member
    vid = member.value
    if assignment.volunteer_id == vid:
        return invalid("this is your own slot")
    already = await session.scalar(
        sa.select(EventAssignment.id).where(
            EventAssignment.event_id == event.id,
            EventAssignment.volunteer_id == vid,
        )
    )
    if already is not None:
        return invalid("you already serve at this event")
    asker = await session.get(Volunteer, assignment.volunteer_id)
    assert asker is not None  # FK guarantees the row
    result = await session.execute(
        sa.update(EventSubRequest)
        .where(
            EventSubRequest.id == sub_request_id,
            EventSubRequest.status == SubRequestStatus.open.value,
        )
        .values(
            status=SubRequestStatus.claimed.value,
            claimed_by_volunteer_id=vid,
            resolved_at=now,
        )
    )
    if result.rowcount == 0:
        return invalid("someone else already claimed this slot")
    assignment.volunteer_id = vid
    assignment.kind = AssignmentKind.sub.value
    # the new person still needs the reminders, on the app's defaults — not
    # the outgoing volunteer's choices, which were theirs and not this one's
    assignment.notify_7d, assignment.notify_24h = False, True
    await session.flush()
    await _mark_notified(session, assignment.id, NotificationStage.event_scheduled)
    await _reset_reminders(session, assignment.id)  # the claimant has had none
    await session.refresh(sub)
    slot = await session.get(EventSlot, assignment.slot_id)
    claimant = await session.get(Volunteer, vid)
    return Ok(
        Outcome(
            (sub, assignment, asker),
            (
                SubClaimed(
                    event_id=event.id,
                    sub_request_id=sub.id,
                    title=event.title,
                    slot=slot.name if slot else "volunteer",
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    claimant=claimant.full_name if claimant else "A teammate",
                    asker=asker.full_name,
                    asker_email=asker.email,
                ),
            ),
        )
    )


async def substitute(
    session: AsyncSession,
    actor: Actor | None,
    *,
    assignment_id: int,
    new_volunteer_id: int,
    acted_by: int | None,
    notify: NotifyMode,
    now: datetime,
) -> Result[Outcome[tuple[EventAssignment, Volunteer, Volunteer]], DomainError]:
    """Hand a slot directly to a chosen teammate — the claim flow minus the
    open call. Any open substitute request on the assignment is cancelled in
    the same transaction. Returns (assignment, outgoing, incoming); the
    SlotHandedOver event is what the policy logs and, in `direct` mode,
    mails to the person now on the hook.

    The incoming volunteer did not act, so they must be told they are now
    scheduled. `notify` says how (domain.NotifyMode): `direct` when the caller
    mails them a substitution notice right after commit (the GUI) -- the
    digest's "scheduled" notice is then stamped as sent to avoid a duplicate;
    `digest` for a caller that sends no mail (the JSON API) -- any stale
    "scheduled" stamp the outgoing person's assignment carried is cleared so
    the nightly digest tells the new person, matching how a manager `assign`
    reaches its volunteer."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        return not_found("assignment", assignment_id)
    found = await _get(session, assignment.event_id)
    if isinstance(found, Err):
        return found
    event = found.value
    if denied := _require_own_or_managed(
        actor, assignment, event, "hand over this slot"
    ):
        return denied
    if closed := _require_open(event, "substitute on this event", now):
        return closed
    member = await _require_member(session, new_volunteer_id, event.team_id)
    if isinstance(member, Err):
        return member
    vid = member.value
    if assignment.volunteer_id == vid:
        return invalid("they already hold this slot")
    already = await session.scalar(
        sa.select(EventAssignment.id).where(
            EventAssignment.event_id == event.id,
            EventAssignment.volunteer_id == vid,
        )
    )
    if already is not None:
        return invalid("they already serve at this event")
    outgoing = await session.get(Volunteer, assignment.volunteer_id)
    incoming = await session.get(Volunteer, vid)
    assert outgoing is not None and incoming is not None  # FKs guarantee the rows
    await session.execute(
        sa.update(EventSubRequest)
        .where(
            EventSubRequest.assignment_id == assignment_id,
            EventSubRequest.status == SubRequestStatus.open.value,
        )
        .values(status=SubRequestStatus.cancelled.value, resolved_at=now)
    )
    assignment.volunteer_id = vid
    assignment.kind = AssignmentKind.sub.value
    assignment.assigned_by = acted_by
    # the new person still needs the reminders, on the app's defaults — not
    # the outgoing volunteer's choices, which were theirs and not this one's
    assignment.notify_7d, assignment.notify_24h = False, True
    await session.flush()
    if notify is NotifyMode.direct:
        # the caller mails the incoming volunteer right after commit, so the
        # digest's "scheduled" notice would only duplicate it
        await _mark_notified(session, assignment.id, NotificationStage.event_scheduled)
    else:
        # no direct mail from this caller: clear any "scheduled" stamp the
        # outgoing person's assignment carried so the nightly digest reaches the
        # incoming volunteer (a stale stamp would otherwise silence it)
        await session.execute(
            sa.delete(Notification).where(
                Notification.assignment_id == assignment.id,
                Notification.stage == NotificationStage.event_scheduled,
            )
        )
    await _reset_reminders(session, assignment.id)
    slot = await session.get(EventSlot, assignment.slot_id)
    return Ok(
        Outcome(
            (assignment, outgoing, incoming),
            (
                SlotHandedOver(
                    event_id=event.id,
                    assignment_id=assignment.id,
                    title=event.title,
                    slot=slot.name if slot else "volunteer",
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    outgoing_id=outgoing.id,
                    outgoing_name=outgoing.full_name,
                    incoming_id=incoming.id,
                    incoming_email=incoming.email,
                    notify=notify,
                ),
            ),
        )
    )


async def cancel_sub(
    session: AsyncSession, actor: Actor | None, sub_request_id: int, *, now: datetime
) -> Result[EventSubRequest, DomainError]:
    """Withdraw an open substitution call — the assignee who opened it, or a
    manager of the event's team."""
    sub = await session.get(EventSubRequest, sub_request_id)
    if sub is None:
        return not_found("substitute request", sub_request_id)
    assignment = await session.get(EventAssignment, sub.assignment_id)
    if assignment is None:  # pragma: no cover - CASCADE removes sub with it
        return not_found("assignment")
    found = await _get(session, assignment.event_id)
    if isinstance(found, Err):
        return found
    if denied := _require_own_or_managed(
        actor, assignment, found.value, "withdraw someone else's substitute request"
    ):
        return denied
    if sub.status != SubRequestStatus.open.value:
        return invalid(f"request is already {sub.status}")
    sub.status = SubRequestStatus.cancelled.value
    sub.resolved_at = now
    await session.flush()
    return Ok(sub)


# --- attendance & hours -------------------------------------------------------


async def set_attendance(
    session: AsyncSession,
    actor: Actor | None,
    *,
    assignment_id: int,
    attended: bool | None,
    hours: Decimal | None,
    now: datetime,
) -> Result[EventAssignment, DomainError]:
    """Record a manager's exception to auto attendance (None clears an
    override back to auto). Only past, non-cancelled events have attendance."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        return not_found("assignment", assignment_id)
    managed = await _managed(session, actor, assignment.event_id)
    if isinstance(managed, Err):
        return managed
    event = managed.value
    if event.status != EventStatus.scheduled.value:
        return invalid("a cancelled event has no attendance")
    if not is_past(event, now):
        return invalid("attendance is recorded after the event ends")
    if hours is not None and hours < 0:
        return invalid("hours cannot be negative")
    assignment.attended_override = attended
    assignment.hours_override = hours
    await session.flush()
    return Ok(assignment)


async def attendance_rows(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> Result[list[tuple[EventAssignment, EventSlot, Volunteer]], DomainError]:
    """The attendance sheet — a manager's view of who turned up."""
    managed = await _managed(session, actor, event_id)
    if isinstance(managed, Err):
        return managed
    rows = (
        await session.execute(
            sa.select(EventAssignment, EventSlot, Volunteer)
            .join(EventSlot, EventSlot.id == EventAssignment.slot_id)
            .join(Volunteer, Volunteer.id == EventAssignment.volunteer_id)
            .where(EventAssignment.event_id == event_id)
            .order_by(
                EventSlot.position,
                EventSlot.id,
                Volunteer.last_name,
                Volunteer.first_name,
            )
        )
    ).all()
    return Ok([(a, s, v) for a, s, v in rows])


async def hours_for_volunteer(
    session: AsyncSession, actor: Actor | None, volunteer_id: int, *, now: datetime
) -> Result[HoursSummary, DomainError]:
    """Derived service record over past, non-cancelled events."""
    if denied := require(
        actor is None
        or actor.can_view_volunteer(
            volunteer_id, await volunteer_team_ids(session, volunteer_id)
        ),
        "view this volunteer's service record",
    ):
        return denied
    rows = (
        await session.execute(
            sa.select(EventAssignment, Event)
            .join(Event, Event.id == EventAssignment.event_id)
            .where(
                EventAssignment.volunteer_id == volunteer_id,
                Event.status == EventStatus.scheduled.value,
                Event.ends_at <= now,
            )
        )
    ).all()
    total = Decimal("0.00")
    attended_count = 0
    for assignment, event in rows:
        attended, hours = effective(assignment, event)
        if attended:
            attended_count += 1
            total += hours
    return Ok(HoursSummary(total_hours=total, events_attended=attended_count))
