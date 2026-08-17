"""Events: team-attached occasions with slots, RSVPs, substitutions and
derived attendance.

Every event belongs to one team; management rights are can_manage_team on
that team, viewing follows the roster-names rule, and taking part (RSVP,
sign-up, claiming a substitution) additionally requires actual membership —
enforced inside the service as a domain invariant.

Unlike the GUI, these mutations send NO email (the repo precedent: mail goes
out from UI handlers after commit, and from the nightly digest). An
API-created assignment still reaches its volunteer through
jobs.event_reminders' "you have been scheduled" notice.
"""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Query

from ..models import Event, EventSlot, EventStatus, EventSubRequest, Volunteer
from ..permissions import require
from ..services import events as event_service
from .deps import CtxDep
from .schemas import (
    AttendanceIn,
    AttendanceRowOut,
    EventAssignIn,
    EventAssignmentOut,
    EventCreateIn,
    EventDetailOut,
    EventOut,
    EventPatch,
    EventRsvpIn,
    EventRsvpOut,
    EventSlotIn,
    EventSlotOut,
    EventSlotPatch,
    EventSummaryOut,
    SlotViewOut,
    SubRequestIn,
    SubRequestOut,
)

router = APIRouter(prefix="/events", tags=["events"])


async def _get_or_404(ctx: CtxDep, event_id: int) -> Event:
    event = await event_service.get(ctx.session, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")
    return event


async def _managed(ctx: CtxDep, event_id: int) -> Event:
    event = await _get_or_404(ctx, event_id)
    require(ctx.actor.can_manage_team(event.team_id), "manage this team's events")
    return event


def _detail_out(
    view: event_service.EventDetail, sub_wanted: set[int]
) -> EventDetailOut:
    slots = []
    for sv in view.slots:
        entries = []
        for assignment, volunteer in sv.entries:
            out = EventAssignmentOut.model_validate(assignment)
            out.volunteer_name = volunteer.full_name
            out.sub_requested = assignment.id in sub_wanted
            entries.append(out)
        slots.append(
            SlotViewOut(
                slot=EventSlotOut.model_validate(sv.slot),
                entries=entries,
                open_spots=sv.open_spots,
            )
        )
    rsvps = []
    for rsvp, volunteer in view.rsvps:
        out = EventRsvpOut.model_validate(rsvp)
        out.volunteer_name = volunteer.full_name
        rsvps.append(out)
    return EventDetailOut(
        event=EventOut.model_validate(view.event),
        path=view.path,
        slots=slots,
        rsvps=rsvps,
    )


@router.get("")
async def list_events(
    ctx: CtxDep,
    team_id: int | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    include_cancelled: bool = False,
) -> list[EventSummaryOut]:
    """Events the caller may see — admins all, everyone else the teams where
    they have at least roster-name rights (scoped inside the service)."""
    summaries = await event_service.list_events(
        ctx.session,
        ctx.actor,
        team_id=team_id,
        from_=from_,
        to=to,
        include_cancelled=include_cancelled,
    )
    return [
        EventSummaryOut(
            event=EventOut.model_validate(s.event),
            path=s.path,
            filled=s.filled,
            capacity=s.capacity,
            my_assignment_id=s.my_assignment.id if s.my_assignment else None,
            my_rsvp_available=s.my_rsvp.available if s.my_rsvp else None,
        )
        for s in summaries
    ]


@router.post("", status_code=201)
async def create_event(ctx: CtxDep, data: EventCreateIn) -> list[EventOut]:
    """One event — or one per week through repeat_weekly_until (inclusive),
    each with its own copy of the slots."""
    require(ctx.actor.can_manage_team(data.team_id), "manage this team's events")
    events = await event_service.create_event(
        ctx.session,
        team_id=data.team_id,
        title=data.title,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        description=data.description,
        location=data.location,
        slots=[
            event_service.SlotInput(s.name, s.capacity, s.position) for s in data.slots
        ],
        repeat_weekly_until=data.repeat_weekly_until,
        created_by=ctx.actor.user.id,
    )
    return [EventOut.model_validate(e) for e in events]


@router.get("/{event_id}")
async def event_detail(ctx: CtxDep, event_id: int) -> EventDetailOut:
    event = await _get_or_404(ctx, event_id)
    require(ctx.actor.can_view_roster_names(event.team_id), "view this team's events")
    view = await event_service.detail(ctx.session, event_id)
    out = _detail_out(view, {a.id for _, a in view.open_subs})
    if ctx.actor.can_manage_team(event.team_id) and event_service.is_past(event):
        if event.status == EventStatus.scheduled.value:
            rows = await event_service.attendance_rows(ctx.session, event_id)
            out.attendance = [
                AttendanceRowOut(
                    assignment_id=a.id,
                    volunteer_id=v.id,
                    volunteer_name=v.full_name,
                    slot_name=s.name,
                    attended=att,
                    hours=float(hours),
                    overridden=a.attended_override is not None
                    or a.hours_override is not None,
                )
                for a, s, v in rows
                for att, hours in [event_service.effective(a, event)]
            ]
    return out


@router.patch("/{event_id}")
async def update_event(ctx: CtxDep, event_id: int, data: EventPatch) -> EventOut:
    """Edit details/times; allowed on past events (a corrected end time
    recomputes the auto hours) but not on cancelled ones."""
    await _managed(ctx, event_id)
    event = await event_service.update_event(
        ctx.session, event_id, **data.model_dump(exclude_unset=True)
    )
    return EventOut.model_validate(event)


@router.post("/{event_id}/cancel")
async def cancel_event(ctx: CtxDep, event_id: int) -> EventOut:
    await _managed(ctx, event_id)
    event, _emails = await event_service.cancel_event(
        ctx.session, event_id, cancelled_by=ctx.actor.user.id
    )
    return EventOut.model_validate(event)


# --- slots --------------------------------------------------------------------


@router.post("/{event_id}/slots", status_code=201)
async def add_slot(ctx: CtxDep, event_id: int, data: EventSlotIn) -> EventSlotOut:
    await _managed(ctx, event_id)
    slot = await event_service.add_slot(
        ctx.session,
        event_id,
        name=data.name,
        capacity=data.capacity,
        position=data.position,
    )
    return EventSlotOut.model_validate(slot)


@router.patch("/{event_id}/slots/{slot_id}")
async def update_slot(
    ctx: CtxDep, event_id: int, slot_id: int, data: EventSlotPatch
) -> EventSlotOut:
    await _managed(ctx, event_id)
    await _slot_of(ctx, event_id, slot_id)
    slot = await event_service.update_slot(
        ctx.session, slot_id, **data.model_dump(exclude_unset=True)
    )
    return EventSlotOut.model_validate(slot)


@router.delete("/{event_id}/slots/{slot_id}", status_code=204)
async def delete_slot(ctx: CtxDep, event_id: int, slot_id: int) -> None:
    await _managed(ctx, event_id)
    await _slot_of(ctx, event_id, slot_id)
    await event_service.delete_slot(ctx.session, slot_id)


async def _slot_of(ctx: CtxDep, event_id: int, slot_id: int) -> None:
    slot = await ctx.session.get(EventSlot, slot_id)
    if slot is None or slot.event_id != event_id:
        raise LookupError(f"slot {slot_id} not found")


# --- taking part --------------------------------------------------------------


@router.put("/{event_id}/rsvp", status_code=204)
async def set_rsvp(ctx: CtxDep, event_id: int, data: EventRsvpIn) -> None:
    """Idempotent overwrite of the caller's availability answer (membership
    of the event's team is enforced in the service)."""
    await _get_or_404(ctx, event_id)
    await event_service.set_rsvp(
        ctx.session,
        event_id=event_id,
        volunteer_id=ctx.actor.volunteer_id,
        available=data.available,
        note=data.note,
    )


@router.post("/{event_id}/slots/{slot_id}/assignments", status_code=201)
async def create_assignment(
    ctx: CtxDep, event_id: int, slot_id: int, data: EventAssignIn
) -> EventAssignmentOut:
    """No volunteer_id: sign yourself up. With one: a manager schedules
    that team member."""
    event = await _get_or_404(ctx, event_id)
    await _slot_of(ctx, event_id, slot_id)
    if data.volunteer_id is None:
        if data.repeat_series:
            assignment, _ = await event_service.sign_up_series(
                ctx.session,
                slot_id=slot_id,
                volunteer_id=ctx.actor.volunteer_id,
                notify_7d=data.notify_7d,
                notify_24h=data.notify_24h,
            )
        else:
            assignment = await event_service.sign_up(
                ctx.session,
                slot_id=slot_id,
                volunteer_id=ctx.actor.volunteer_id,
                notify_7d=data.notify_7d,
                notify_24h=data.notify_24h,
            )
    else:
        if data.repeat_series:
            raise ValueError("repeat_series applies to self sign-ups only")
        require(ctx.actor.can_manage_team(event.team_id), "manage this team's events")
        assignment = await event_service.assign(
            ctx.session,
            slot_id=slot_id,
            volunteer_id=data.volunteer_id,
            assigned_by=ctx.actor.user.id,
        )
    return EventAssignmentOut.model_validate(assignment)


@router.delete("/assignments/{assignment_id}", status_code=204)
async def remove_assignment(ctx: CtxDep, assignment_id: int) -> None:
    """Withdraw yourself, or (as a manager) remove anyone — future events
    only; past rosters are the attendance record."""
    assignment = await event_service.get_assignment(ctx.session, assignment_id)
    if assignment is None:
        raise LookupError(f"assignment {assignment_id} not found")
    event = await _get_or_404(ctx, assignment.event_id)
    require(
        assignment.volunteer_id == ctx.actor.volunteer_id
        or ctx.actor.can_manage_team(event.team_id),
        "change other people's assignments",
    )
    await event_service.remove_assignment(ctx.session, assignment_id)


# --- substitutions ------------------------------------------------------------


@router.post("/assignments/{assignment_id}/sub-request", status_code=201)
async def request_sub(
    ctx: CtxDep, assignment_id: int, data: SubRequestIn
) -> SubRequestOut:
    """Open a substitution call for your own assignment (or, as a manager,
    anyone's). NOTE: unlike the GUI, no teammate email goes out."""
    assignment = await event_service.get_assignment(ctx.session, assignment_id)
    if assignment is None:
        raise LookupError(f"assignment {assignment_id} not found")
    event = await _get_or_404(ctx, assignment.event_id)
    require(
        assignment.volunteer_id == ctx.actor.volunteer_id
        or ctx.actor.can_manage_team(event.team_id),
        "ask for a substitute for someone else",
    )
    sub = await event_service.request_sub(
        ctx.session,
        assignment_id=assignment_id,
        requested_by=ctx.actor.user.id,
        note=data.note,
    )
    return SubRequestOut.model_validate(sub)


@router.post("/sub-requests/{sub_request_id}/claim")
async def claim_sub(ctx: CtxDep, sub_request_id: int) -> SubRequestOut:
    """First-come claim; the assignment moves to the caller."""
    sub, _assignment, _asker = await event_service.claim_sub(
        ctx.session, sub_request_id=sub_request_id, volunteer_id=ctx.actor.volunteer_id
    )
    return SubRequestOut.model_validate(sub)


@router.post("/sub-requests/{sub_request_id}/cancel")
async def cancel_sub(ctx: CtxDep, sub_request_id: int) -> SubRequestOut:
    sub = await ctx.session.get(EventSubRequest, sub_request_id)
    if sub is None:
        raise LookupError(f"substitute request {sub_request_id} not found")
    assignment = await event_service.get_assignment(ctx.session, sub.assignment_id)
    event = await _get_or_404(ctx, assignment.event_id) if assignment else None
    require(
        (assignment is not None and assignment.volunteer_id == ctx.actor.volunteer_id)
        or (event is not None and ctx.actor.can_manage_team(event.team_id)),
        "withdraw someone else's substitute request",
    )
    sub = await event_service.cancel_sub(ctx.session, sub_request_id)
    return SubRequestOut.model_validate(sub)


# --- attendance ---------------------------------------------------------------


@router.patch("/assignments/{assignment_id}/attendance")
async def set_attendance(
    ctx: CtxDep, assignment_id: int, data: AttendanceIn
) -> AttendanceRowOut:
    """Record an exception to auto attendance (nulls clear back to auto).
    Past, non-cancelled events only."""
    assignment = await event_service.get_assignment(ctx.session, assignment_id)
    if assignment is None:
        raise LookupError(f"assignment {assignment_id} not found")
    event = await _get_or_404(ctx, assignment.event_id)
    require(ctx.actor.can_manage_team(event.team_id), "record attendance")
    assignment = await event_service.set_attendance(
        ctx.session,
        assignment_id=assignment_id,
        attended=data.attended,
        hours=Decimal(str(data.hours)) if data.hours is not None else None,
    )
    attended, hours = event_service.effective(assignment, event)
    slot = await ctx.session.get(EventSlot, assignment.slot_id)
    volunteer = await ctx.session.get(Volunteer, assignment.volunteer_id)
    return AttendanceRowOut(
        assignment_id=assignment.id,
        volunteer_id=assignment.volunteer_id,
        volunteer_name=volunteer.full_name if volunteer else "",
        slot_name=slot.name if slot else "",
        attended=attended,
        hours=float(hours),
        overridden=assignment.attended_override is not None
        or assignment.hours_override is not None,
    )
