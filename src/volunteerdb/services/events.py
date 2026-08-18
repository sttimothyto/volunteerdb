"""Scheduling: events, slots, RSVPs, substitutions, attendance.

An event always belongs to exactly one team; membership of that team is the
domain gate for RSVPs, sign-ups, assignments and substitution claims (a
parish-wide occasion gets its own task-force team first). Like every
service, *permission* checks (who may manage, who may view) live in the
callers via require() — but team membership is enforced here as a domain
invariant, the way elections checks a candidate belongs to its proposal.

Attendance is derived, not entered: an assignment on a past, non-cancelled
event counts as attended for the scheduled duration unless a manager
recorded attended_override / hours_override (see effective()).

sign_up/assign take the slot row FOR UPDATE before counting occupants — the
first use of a row lock in this codebase, because a *counted* capacity has
no partial-unique-index backstop the way one-open-per-seat rules do.
"""

import difflib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    AssignmentKind,
    Event,
    EventAssignment,
    EventRsvp,
    EventSlot,
    EventStatus,
    EventSubRequest,
    Membership,
    SubRequestStatus,
    Team,
    Volunteer,
)
from ..permissions import Actor, require, volunteer_team_ids
from . import teams as team_service

_UNSET: object = object()

# copy-forward bound: one year of weekly occurrences is the most a single
# create should materialize (concrete rows, no recurrence engine)
MAX_REPEAT_DAYS = 366

TWO_PLACES = Decimal("0.01")


# --- view models --------------------------------------------------------------


@dataclass(frozen=True)
class SlotInput:
    name: str
    capacity: int | None = None  # None = unlimited
    position: int = 0


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


def _now() -> datetime:
    return datetime.now(UTC)


def is_past(event: Event, now: datetime | None = None) -> bool:
    return event.ends_at <= (now or _now())


def _require_open(event: Event, action: str) -> None:
    """Roster mutations freeze once the event ended (attendance overrides are
    the only post-event lever) and are refused on cancelled events."""
    if event.status != EventStatus.scheduled.value:
        raise ValueError(f"cannot {action}: event is cancelled")
    if is_past(event):
        raise ValueError(f"cannot {action}: event has already ended")


# --- events -------------------------------------------------------------------


def _check_times(starts_at: datetime, ends_at: datetime) -> None:
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise ValueError("event times must carry a timezone")
    if starts_at >= ends_at:
        raise ValueError("the event must end after it starts")


def _check_slots(slots: list[SlotInput]) -> None:
    names = [s.name.strip() for s in slots]
    if any(not n for n in names):
        raise ValueError("slot names cannot be empty")
    if len(set(names)) != len(names):
        raise ValueError("slot names must be unique within the event")
    if any(s.capacity is not None and s.capacity < 1 for s in slots):
        raise ValueError("slot capacity must be at least 1 (or blank for unlimited)")


def _occurrences(
    starts_at: datetime, ends_at: datetime, until: date | None
) -> list[tuple[datetime, datetime]]:
    """Weekly copy-forward. Wall-clock times repeat in the parish's timezone:
    the dates advance by 7 days and recombine with the local clock time, so a
    10:30 Mass stays 10:30 across a DST change (never timedelta(weeks=1) on
    the instant, which would shift it an hour)."""
    if until is None:
        return [(starts_at, ends_at)]
    tz = ZoneInfo(settings().timezone)
    local_start = starts_at.astimezone(tz)
    local_end = ends_at.astimezone(tz)
    if until < local_start.date():
        raise ValueError("the repeat-until date is before the first event")
    if until > local_start.date() + timedelta(days=MAX_REPEAT_DAYS):
        raise ValueError("repeats are limited to one year of events")
    span_days = (local_end.date() - local_start.date()).days
    out: list[tuple[datetime, datetime]] = []
    day = local_start.date()
    while day <= until:
        s = datetime.combine(day, local_start.time(), tz)
        e = datetime.combine(day + timedelta(days=span_days), local_end.time(), tz)
        out.append((s, e))
        day += timedelta(days=7)
    return out


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
) -> list[Event]:
    """Create one event — or, with repeat_weekly_until, one per week through
    that date (inclusive), each with its own copy of the slots. No slots
    given means one unlimited "Volunteers" slot."""
    require(
        actor is None or actor.can_manage_team(team_id),
        "manage this team's events",
    )
    title = title.strip()
    if not title:
        raise ValueError("a title is required")
    _check_times(starts_at, ends_at)
    if await session.get(Team, team_id) is None:
        raise LookupError(f"team {team_id} not found")
    slot_inputs = slots or [SlotInput("Volunteers")]
    _check_slots(slot_inputs)

    # weekly repeats share a series id (even a range that yields one row —
    # it was created AS a series); standalone events carry NULL
    series_id = uuid4() if repeat_weekly_until is not None else None
    events: list[Event] = []
    for s, e in _occurrences(starts_at, ends_at, repeat_weekly_until):
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
                )
            )
    await session.flush()
    return events


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
) -> Event:
    """Edit details/times. Allowed on past events (a manager correcting a
    wrong end time legitimately recomputes the auto hours) but not on
    cancelled ones."""
    event = await _managed(session, actor, event_id)
    if event.status != EventStatus.scheduled.value:
        raise ValueError("cannot edit: event is cancelled")
    if title is not _UNSET:
        if not str(title).strip():
            raise ValueError("a title is required")
        event.title = str(title).strip()
    if description is not _UNSET:
        event.description = description or None  # type: ignore[assignment]
    if location is not _UNSET:
        event.location = (str(location or "")).strip() or None
    new_start = event.starts_at if starts_at is _UNSET else starts_at
    new_end = event.ends_at if ends_at is _UNSET else ends_at
    if starts_at is not _UNSET or ends_at is not _UNSET:
        _check_times(new_start, new_end)  # type: ignore[arg-type]
        event.starts_at = new_start  # type: ignore[assignment]
        event.ends_at = new_end  # type: ignore[assignment]
    await session.flush()
    return event


async def cancel_event(
    session: AsyncSession,
    actor: Actor | None,
    event_id: int,
    *,
    cancelled_by: int | None,
) -> tuple[Event, list[str]]:
    """Cancel the event and resolve its open substitution requests in the
    same transaction. Returns the assignees' emails so the caller can tell
    them AFTER the transaction commits (mail never rides a transaction)."""
    event = await _managed(session, actor, event_id)
    if event.status != EventStatus.scheduled.value:
        raise ValueError("event is already cancelled")
    event.status = EventStatus.cancelled.value
    event.cancelled_at = _now()
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
        .values(status=SubRequestStatus.cancelled.value, resolved_at=sa.func.now())
    )
    emails = list(
        await session.scalars(
            sa.select(sa.distinct(Volunteer.email))
            .join(EventAssignment, EventAssignment.volunteer_id == Volunteer.id)
            .where(EventAssignment.event_id == event_id, Volunteer.email.is_not(None))
        )
    )
    await session.flush()
    return event, sorted(emails)


async def get(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id)


async def _get_or_raise(session: AsyncSession, event_id: int) -> Event:
    event = await session.get(Event, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")
    return event


# --- slots --------------------------------------------------------------------


async def _managed(session: AsyncSession, actor: Actor | None, event_id: int) -> Event:
    """The event, for a caller who runs its team.

    Managing an event — its details, slots, cancellation, other people's
    assignments, attendance — is can_manage_team on the owning team. For a
    task-force event that is the meta team, which permissions.Actor keeps in
    the managing scope precisely so collaboration works.
    """
    event = await _get_or_raise(session, event_id)
    require(
        actor is None or actor.can_manage_team(event.team_id),
        "manage this team's events",
    )
    return event


async def _visible(session: AsyncSession, actor: Actor | None, event_id: int) -> Event:
    """The event, for a caller who may see the owning team's roster names —
    the documented visibility domain for an event and its assignee list.

    Taking PART is stricter and separate: _require_member enforces real
    membership of the team as a domain invariant, admin or not.
    """
    event = await _get_or_raise(session, event_id)
    require(
        actor is None or actor.can_view_roster_names(event.team_id),
        "view this team's events",
    )
    return event


async def add_slot(
    session: AsyncSession,
    actor: Actor | None,
    event_id: int,
    *,
    name: str,
    capacity: int | None = None,
    position: int = 0,
) -> EventSlot:
    event = await _managed(session, actor, event_id)
    _require_open(event, "add a slot")
    _check_slots([SlotInput(name, capacity, position)])
    existing = await session.scalar(
        sa.select(EventSlot.id).where(
            EventSlot.event_id == event_id, EventSlot.name == name.strip()
        )
    )
    if existing is not None:
        raise ValueError("a slot with that name already exists")
    slot = EventSlot(
        event_id=event_id, name=name.strip(), capacity=capacity, position=position
    )
    session.add(slot)
    await session.flush()
    return slot


async def update_slot(
    session: AsyncSession,
    actor: Actor | None,
    slot_id: int,
    *,
    name: str | object = _UNSET,
    capacity: int | None | object = _UNSET,
    position: int | object = _UNSET,
) -> EventSlot:
    slot = await session.get(EventSlot, slot_id)
    if slot is None:
        raise LookupError(f"slot {slot_id} not found")
    event = await _managed(session, actor, slot.event_id)
    _require_open(event, "edit a slot")
    if name is not _UNSET:
        if not str(name).strip():
            raise ValueError("slot names cannot be empty")
        slot.name = str(name).strip()
    if capacity is not _UNSET:
        if capacity is not None and int(capacity) < 1:  # type: ignore[arg-type]
            raise ValueError(
                "slot capacity must be at least 1 (or blank for unlimited)"
            )
        if capacity is not None:
            occupied = await session.scalar(
                sa.select(sa.func.count()).where(EventAssignment.slot_id == slot.id)
            )
            if occupied is not None and occupied > int(capacity):  # type: ignore[arg-type]
                raise ValueError(f"{occupied} people already fill this slot")
        slot.capacity = capacity  # type: ignore[assignment]
    if position is not _UNSET:
        slot.position = int(position)  # type: ignore[arg-type]
    await session.flush()
    return slot


async def delete_slot(
    session: AsyncSession, actor: Actor | None, slot_id: int
) -> Event:
    slot = await session.get(EventSlot, slot_id)
    if slot is None:
        raise LookupError(f"slot {slot_id} not found")
    event = await _managed(session, actor, slot.event_id)
    _require_open(event, "remove a slot")
    occupied = await session.scalar(
        sa.select(sa.func.count()).where(EventAssignment.slot_id == slot.id)
    )
    if occupied:
        raise ValueError("remove its people first — the slot is not empty")
    remaining = await session.scalar(
        sa.select(sa.func.count()).where(EventSlot.event_id == event.id)
    )
    if remaining is not None and remaining <= 1:
        raise ValueError("an event needs at least one slot")
    await session.delete(slot)
    await session.flush()
    return event


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
    paths = team_service.team_paths(await team_service.list_all(session))

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
) -> list[SimilarEvent]:
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
    wanted = _norm_location(location or "")
    if not wanted:
        return []
    tz = ZoneInfo(settings().timezone)
    day_set: set[date] = set()
    for occ_start, occ_end in _occurrences(starts_at, ends_at, repeat_until):
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
    paths = team_service.team_paths(await team_service.list_all(session))
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
    return out


async def detail(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> EventDetail:
    """One event with its slots, entries, RSVPs and open substitution calls.

    Roster-names rights on the owning team, which is the documented visibility
    domain for an event and the names of who is staffing it."""
    event = await _visible(session, actor, event_id)
    paths = team_service.team_paths(await team_service.list_all(session))
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
    return EventDetail(
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


async def my_upcoming(session: AsyncSession, volunteer_id: int) -> list[MyDuty]:
    rows = (
        await session.execute(
            sa.select(EventAssignment, Event, EventSlot)
            .join(Event, Event.id == EventAssignment.event_id)
            .join(EventSlot, EventSlot.id == EventAssignment.slot_id)
            .where(
                EventAssignment.volunteer_id == volunteer_id,
                Event.status == EventStatus.scheduled.value,
                Event.ends_at > sa.func.now(),
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


async def claimable_subs(session: AsyncSession, actor: Actor) -> list[ClaimableSub]:
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
                Event.ends_at > sa.func.now(),
                EventAssignment.volunteer_id != actor.volunteer_id,
                Event.id.not_in(already_assigned),
            )
            .order_by(Event.starts_at)
        )
    ).all()
    paths = team_service.team_paths(await team_service.list_all(session))
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
) -> None:
    """Your own slot, or a slot on an event you manage.

    Withdrawing, asking for a substitute and cancelling that request are all
    things an assignee does for themselves and a manager may do for anyone on
    their team's event."""
    require(
        actor is None
        or actor.volunteer_id == assignment.volunteer_id
        or actor.can_manage_team(event.team_id),
        what,
    )


def _require_self(actor: Actor | None, volunteer_id: int | None, what: str) -> None:
    """Taking part is something you do for yourself.

    `volunteer_id` reaches these functions from the request, so it is checked
    against the actor rather than trusted: nobody — however privileged — signs
    somebody else up, RSVPs for them, or claims a substitution as them.
    Scheduling another person is a different function (assign), with a
    manager's check on it."""
    require(
        actor is None
        or (volunteer_id is not None and actor.volunteer_id == volunteer_id),
        what,
    )


async def _require_member(
    session: AsyncSession, volunteer_id: int | None, team_id: int
) -> int:
    """The domain gate on every participation write; returns the volunteer id."""
    if volunteer_id is None:
        raise ValueError("your account is not linked to a volunteer record")
    if not await is_member(session, volunteer_id, team_id):
        raise ValueError("only members of the event's team can take part")
    return volunteer_id


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
) -> None:
    """Upsert the volunteer's availability answer (ballot-PUT semantics)."""
    _require_self(actor, volunteer_id, "answer availability for this volunteer")
    event = await _get_or_raise(session, event_id)
    _require_open(event, "answer availability")
    vid = await _require_member(session, volunteer_id, event.team_id)
    note = (note or "").strip()[:200] or None
    await session.execute(
        pg_insert(EventRsvp)
        .values(event_id=event_id, volunteer_id=vid, available=available, note=note)
        .on_conflict_do_update(
            constraint="uq_event_rsvp",
            set_={"available": available, "note": note, "updated_at": sa.func.now()},
        )
    )


# --- assignments --------------------------------------------------------------


async def _join_slot(
    session: AsyncSession,
    *,
    slot_id: int,
    volunteer_id: int,
    kind: AssignmentKind,
    assigned_by: int | None,
    notify_7d: bool = True,
    notify_24h: bool = True,
) -> EventAssignment:
    # FOR UPDATE: a counted capacity has no unique-index backstop, so the two
    # concurrent sign-ups that both counted a free spot must serialize here
    slot = (
        await session.execute(
            sa.select(EventSlot).where(EventSlot.id == slot_id).with_for_update()
        )
    ).scalar_one_or_none()
    if slot is None:
        raise LookupError(f"slot {slot_id} not found")
    event = await _get_or_raise(session, slot.event_id)
    _require_open(event, "join this event")
    await _require_member(session, volunteer_id, event.team_id)
    existing = await session.scalar(
        sa.select(EventAssignment.id).where(
            EventAssignment.event_id == event.id,
            EventAssignment.volunteer_id == volunteer_id,
        )
    )
    if existing is not None:
        raise ValueError("they already serve at this event")
    if slot.capacity is not None:
        occupied = await session.scalar(
            sa.select(sa.func.count()).where(EventAssignment.slot_id == slot.id)
        )
        if occupied is not None and occupied >= slot.capacity:
            raise ValueError(f"{slot.name} is full")
    assignment = EventAssignment(
        slot_id=slot.id,
        event_id=event.id,
        volunteer_id=volunteer_id,
        kind=kind.value,
        assigned_by=assigned_by,
        # the person acted themselves for signup/sub; only manager
        # assignments leave this NULL for the digest's "scheduled" notice
        assigned_notified_at=(None if kind is AssignmentKind.assigned else _now()),
        notify_7d=notify_7d,
        notify_24h=notify_24h,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def sign_up(
    session: AsyncSession,
    actor: Actor | None,
    *,
    slot_id: int,
    volunteer_id: int | None,
    notify_7d: bool = True,
    notify_24h: bool = True,
) -> EventAssignment:
    if volunteer_id is None:
        raise ValueError("your account is not linked to a volunteer record")
    _require_self(actor, volunteer_id, "sign this volunteer up")
    return await _join_slot(
        session,
        slot_id=slot_id,
        volunteer_id=volunteer_id,
        kind=AssignmentKind.signup,
        assigned_by=None,
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
    notify_7d: bool = True,
    notify_24h: bool = True,
) -> tuple[EventAssignment, SeriesSignupResult]:
    """Sign up on the given slot, then copy the sign-up onto every later
    week of the same series with a slot of the same NAME (names are the
    series-wide identity — slot ids differ per materialized row). Each week
    joins inside a SAVEPOINT, so a full week or an existing assignment
    skips that week instead of aborting the whole sign-up."""
    first = await sign_up(
        session,
        actor,
        slot_id=slot_id,
        volunteer_id=volunteer_id,
        notify_7d=notify_7d,
        notify_24h=notify_24h,
    )
    slot = await session.get(EventSlot, first.slot_id)
    event = await session.get(Event, first.event_id)
    assert slot is not None and event is not None  # sign_up just used them
    if event.series_id is None:
        return first, SeriesSignupResult(0, 0, 0)
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
        try:
            async with session.begin_nested():
                await _join_slot(
                    session,
                    slot_id=future_slot_id,
                    volunteer_id=first.volunteer_id,
                    kind=AssignmentKind.signup,
                    assigned_by=None,
                    notify_7d=notify_7d,
                    notify_24h=notify_24h,
                )
            joined += 1
        except ValueError as exc:
            if "full" in str(exc):
                skipped_full += 1
            else:
                skipped_conflict += 1
    return first, SeriesSignupResult(joined, skipped_full, skipped_conflict)


async def assign(
    session: AsyncSession,
    actor: Actor | None,
    *,
    slot_id: int,
    volunteer_id: int,
    assigned_by: int | None,
) -> EventAssignment:
    """Schedule somebody else — a manager's act, unlike sign_up."""
    slot = await session.get(EventSlot, slot_id)
    if slot is None:
        raise LookupError(f"slot {slot_id} not found")
    await _managed(session, actor, slot.event_id)
    return await _join_slot(
        session,
        slot_id=slot_id,
        volunteer_id=volunteer_id,
        kind=AssignmentKind.assigned,
        assigned_by=assigned_by,
    )


async def get_assignment(
    session: AsyncSession, assignment_id: int
) -> EventAssignment | None:
    return await session.get(EventAssignment, assignment_id)


async def remove_assignment(
    session: AsyncSession, actor: Actor | None, assignment_id: int
) -> Event:
    """Withdraw/remove a future assignment (its sub requests cascade away).
    Past rosters are frozen — they are the attendance record."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        raise LookupError(f"assignment {assignment_id} not found")
    event = await _get_or_raise(session, assignment.event_id)
    _require_own_or_managed(actor, assignment, event, "remove this assignment")
    _require_open(event, "leave this event")
    await session.delete(assignment)
    await session.flush()
    return event


# --- substitutions ------------------------------------------------------------


async def request_sub(
    session: AsyncSession,
    actor: Actor | None,
    *,
    assignment_id: int,
    requested_by: int | None,
    note: str | None = None,
) -> EventSubRequest:
    """Open a substitution call. The friendly pre-check keeps repeat clicks
    from re-mailing the team; uq_event_sub_request_open backstops the race."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        raise LookupError(f"assignment {assignment_id} not found")
    event = await _get_or_raise(session, assignment.event_id)
    _require_own_or_managed(actor, assignment, event, "ask for a substitute here")
    _require_open(event, "ask for a substitute")
    existing = await session.scalar(
        sa.select(EventSubRequest.id).where(
            EventSubRequest.assignment_id == assignment_id,
            EventSubRequest.status == SubRequestStatus.open.value,
        )
    )
    if existing is not None:
        raise ValueError("a substitute request is already open for this slot")
    sub = EventSubRequest(
        assignment_id=assignment_id,
        requested_by=requested_by,
        note=(note or "").strip()[:200] or None,
    )
    session.add(sub)
    await session.flush()
    return sub


async def claim_sub(
    session: AsyncSession,
    actor: Actor | None,
    *,
    sub_request_id: int,
    volunteer_id: int | None,
) -> tuple[EventSubRequest, EventAssignment, Volunteer]:
    """First-come claim: the guarded UPDATE ... WHERE status='open' decides
    the race (the loser's rowcount is 0), and the assignment moves to the
    claimant in the same transaction — they commit or roll back together
    (the appoint() pattern). Returns the person who asked, for the caller's
    post-commit mail."""
    sub = await session.get(EventSubRequest, sub_request_id)
    if sub is None:
        raise LookupError(f"substitute request {sub_request_id} not found")
    assignment = await session.get(EventAssignment, sub.assignment_id)
    if assignment is None:  # pragma: no cover - CASCADE removes sub with it
        raise LookupError("the assignment no longer exists")
    event = await _get_or_raise(session, assignment.event_id)
    _require_self(actor, volunteer_id, "claim this slot for this volunteer")
    _require_open(event, "claim this slot")
    vid = await _require_member(session, volunteer_id, event.team_id)
    if assignment.volunteer_id == vid:
        raise ValueError("this is your own slot")
    already = await session.scalar(
        sa.select(EventAssignment.id).where(
            EventAssignment.event_id == event.id,
            EventAssignment.volunteer_id == vid,
        )
    )
    if already is not None:
        raise ValueError("you already serve at this event")
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
            resolved_at=sa.func.now(),
        )
    )
    if result.rowcount == 0:
        raise ValueError("someone else already claimed this slot")
    assignment.volunteer_id = vid
    assignment.kind = AssignmentKind.sub.value
    assignment.assigned_notified_at = _now()  # the claimant acted themselves
    # the new person still needs the reminders, on default preferences
    assignment.notify_7d = assignment.notify_24h = True
    assignment.reminded_7d_at = assignment.reminded_24h_at = None
    await session.flush()
    await session.refresh(sub)
    return sub, assignment, asker


async def substitute(
    session: AsyncSession,
    actor: Actor | None,
    *,
    assignment_id: int,
    new_volunteer_id: int,
    acted_by: int | None,
) -> tuple[EventAssignment, Volunteer, Volunteer]:
    """Hand a slot directly to a chosen teammate — the claim flow minus the
    open call. Any open substitute request on the assignment is cancelled in
    the same transaction. Returns (assignment, outgoing, incoming) for the
    caller's post-commit mail to the person now on the hook."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        raise LookupError(f"assignment {assignment_id} not found")
    event = await _get_or_raise(session, assignment.event_id)
    _require_own_or_managed(actor, assignment, event, "hand over this slot")
    _require_open(event, "substitute on this event")
    vid = await _require_member(session, new_volunteer_id, event.team_id)
    if assignment.volunteer_id == vid:
        raise ValueError("they already hold this slot")
    already = await session.scalar(
        sa.select(EventAssignment.id).where(
            EventAssignment.event_id == event.id,
            EventAssignment.volunteer_id == vid,
        )
    )
    if already is not None:
        raise ValueError("they already serve at this event")
    outgoing = await session.get(Volunteer, assignment.volunteer_id)
    incoming = await session.get(Volunteer, vid)
    assert outgoing is not None and incoming is not None  # FKs guarantee the rows
    await session.execute(
        sa.update(EventSubRequest)
        .where(
            EventSubRequest.assignment_id == assignment_id,
            EventSubRequest.status == SubRequestStatus.open.value,
        )
        .values(status=SubRequestStatus.cancelled.value, resolved_at=sa.func.now())
    )
    assignment.volunteer_id = vid
    assignment.kind = AssignmentKind.sub.value
    assignment.assigned_by = acted_by
    # the caller mails the incoming volunteer right after commit, so the
    # digest's "scheduled" notice would only duplicate it
    assignment.assigned_notified_at = _now()
    # the new person still needs the reminders, on default preferences
    assignment.notify_7d = assignment.notify_24h = True
    assignment.reminded_7d_at = assignment.reminded_24h_at = None
    await session.flush()
    return assignment, outgoing, incoming


async def cancel_sub(
    session: AsyncSession, actor: Actor | None, sub_request_id: int
) -> EventSubRequest:
    """Withdraw an open substitution call — the assignee who opened it, or a
    manager of the event's team."""
    sub = await session.get(EventSubRequest, sub_request_id)
    if sub is None:
        raise LookupError(f"substitute request {sub_request_id} not found")
    assignment = await session.get(EventAssignment, sub.assignment_id)
    if assignment is None:  # pragma: no cover - CASCADE removes sub with it
        raise LookupError("the assignment no longer exists")
    event = await _get_or_raise(session, assignment.event_id)
    _require_own_or_managed(
        actor, assignment, event, "withdraw someone else's substitute request"
    )
    if sub.status != SubRequestStatus.open.value:
        raise ValueError(f"request is already {sub.status}")
    sub.status = SubRequestStatus.cancelled.value
    sub.resolved_at = _now()
    await session.flush()
    return sub


# --- attendance & hours -------------------------------------------------------


async def set_attendance(
    session: AsyncSession,
    actor: Actor | None,
    *,
    assignment_id: int,
    attended: bool | None,
    hours: Decimal | None,
) -> EventAssignment:
    """Record a manager's exception to auto attendance (None clears an
    override back to auto). Only past, non-cancelled events have attendance."""
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None:
        raise LookupError(f"assignment {assignment_id} not found")
    event = await _managed(session, actor, assignment.event_id)
    if event.status != EventStatus.scheduled.value:
        raise ValueError("a cancelled event has no attendance")
    if not is_past(event):
        raise ValueError("attendance is recorded after the event ends")
    if hours is not None and hours < 0:
        raise ValueError("hours cannot be negative")
    assignment.attended_override = attended
    assignment.hours_override = hours
    await session.flush()
    return assignment


async def attendance_rows(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> list[tuple[EventAssignment, EventSlot, Volunteer]]:
    """The attendance sheet — a manager's view of who turned up."""
    await _managed(session, actor, event_id)
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
    return [(a, s, v) for a, s, v in rows]


async def hours_for_volunteer(
    session: AsyncSession, actor: Actor | None, volunteer_id: int
) -> HoursSummary:
    """Derived service record over past, non-cancelled events."""
    require(
        actor is None
        or actor.can_view_volunteer(
            volunteer_id, await volunteer_team_ids(session, volunteer_id)
        ),
        "view this volunteer's service record",
    )
    rows = (
        await session.execute(
            sa.select(EventAssignment, Event)
            .join(Event, Event.id == EventAssignment.event_id)
            .where(
                EventAssignment.volunteer_id == volunteer_id,
                Event.status == EventStatus.scheduled.value,
                Event.ends_at <= sa.func.now(),
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
    return HoursSummary(total_hours=total, events_attended=attended_count)
