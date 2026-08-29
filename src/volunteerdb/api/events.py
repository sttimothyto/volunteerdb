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

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Query

from ..log import audit_log
from ..models import Event, EventSlot, EventStatus, EventSubRequest, Volunteer
from ..permissions import require
from ..services import events as event_service
from ..services import task_force as task_force_service
from ..services import teams as team_service
from .deps import CtxDep
from .schemas import (
    AttendanceIn,
    AttendanceRowOut,
    ClaimableSubOut,
    CollaboratorIn,
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
    MyDutyOut,
    SimilarEventOut,
    SlotViewOut,
    SubRequestIn,
    SubRequestOut,
    SubstituteIn,
    TaskForceOut,
    TeamOut,
    TeamWithPath,
)

router = APIRouter(prefix="/events", tags=["events"])


async def _get_or_404(ctx: CtxDep, event_id: int) -> Event:
    event = await event_service.get(ctx.session, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")
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


@router.get("/mine")
async def my_duties(ctx: CtxDep) -> list[MyDutyOut]:
    """The caller's upcoming commitments, soonest first — the GUI's "My duties"
    list, which had no endpoint. `GET /reports/dashboard` counts them; this
    names them."""
    require(ctx.actor.volunteer_id is not None, "see your own duties")
    duties = await event_service.my_upcoming(
        ctx.session, ctx.actor.volunteer_id, now=ctx.now
    )
    return [
        MyDutyOut(
            assignment_id=d.assignment.id,
            event=EventOut.model_validate(d.event),
            slot_id=d.slot.id,
            slot_name=d.slot.name,
            open_sub_request_id=d.open_sub.id if d.open_sub else None,
        )
        for d in duties
    ]


@router.get("/claimable")
async def claimable(ctx: CtxDep) -> list[ClaimableSubOut]:
    """Open substitution calls the caller could take over: their own teams'
    events, minus the ones they already serve at."""
    require(ctx.actor.volunteer_id is not None, "claim a substitution")
    subs = await event_service.claimable_subs(ctx.session, ctx.actor, now=ctx.now)
    return [
        ClaimableSubOut(
            sub_request_id=c.sub.id,
            assignment_id=c.assignment.id,
            event=EventOut.model_validate(c.event),
            slot_id=c.slot.id,
            slot_name=c.slot.name,
            asked_by_volunteer_id=c.volunteer.id,
            asked_by_name=c.volunteer.full_name,
            note=c.sub.note,
            path=c.path,
        )
        for c in subs
    ]


@router.get("/similar")
async def similar(
    ctx: CtxDep,
    starts_at: datetime,
    ends_at: datetime,
    location: str | None = None,
    repeat_weekly_until: date | None = None,
) -> list[SimilarEventOut]:
    """The advisory double-booking check the GUI runs before creating an event.

    Advisory on purpose: it never blocks, and a hit on a team outside the
    caller's view comes back with `title` null — the when and where is the
    warning, the details stay that team's. Call it before POST /events to give
    the same warning the create dialog gives.
    """
    require(ctx.actor.can_create_events, "create events")
    hits = (
        await event_service.similar_events(
            ctx.session,
            ctx.actor,
            starts_at=starts_at,
            ends_at=ends_at,
            repeat_until=repeat_weekly_until,
            location=location,
            tz=ctx.env.tz,
        )
    ).unwrap()
    return [
        SimilarEventOut(
            starts_at=h.starts_at,
            ends_at=h.ends_at,
            location=h.location,
            team_path=h.team_path,
            title=h.title,
        )
        for h in hits
    ]


@router.post("", status_code=201)
async def create_event(ctx: CtxDep, data: EventCreateIn) -> list[EventOut]:
    """One event — or one per week through repeat_weekly_until (inclusive),
    each with its own copy of the slots."""
    events = (
        await event_service.create_event(
            ctx.session,
            ctx.actor,
            team_id=data.team_id,
            title=data.title,
            starts_at=data.starts_at,
            ends_at=data.ends_at,
            description=data.description,
            location=data.location,
            slots=[
                event_service.SlotInput(s.name, s.capacity, s.position, s.description)
                for s in data.slots
            ],
            repeat_weekly_until=data.repeat_weekly_until,
            created_by=ctx.actor.user.id,
            tz=ctx.env.tz,
            series_id=ctx.env.rng.uuid(),
        )
    ).unwrap()
    return [EventOut.model_validate(e) for e in events]


@router.get("/{event_id}")
async def event_detail(ctx: CtxDep, event_id: int) -> EventDetailOut:
    event = await _get_or_404(ctx, event_id)
    view = (await event_service.detail(ctx.session, ctx.actor, event_id)).unwrap()
    out = _detail_out(view, {a.id for _, a in view.open_subs})
    if ctx.actor.can_manage_team(event.team_id) and event_service.is_past(
        event, now=ctx.now
    ):
        if event.status == EventStatus.scheduled.value:
            rows = (
                await event_service.attendance_rows(ctx.session, ctx.actor, event_id)
            ).unwrap()
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
    event = (
        await event_service.update_event(
            ctx.session, ctx.actor, event_id, **data.model_dump(exclude_unset=True)
        )
    ).unwrap()
    return EventOut.model_validate(event)


@router.post("/{event_id}/cancel")
async def cancel_event(ctx: CtxDep, event_id: int) -> EventOut:
    event, _emails = (
        await event_service.cancel_event(
            ctx.session,
            ctx.actor,
            event_id,
            cancelled_by=ctx.actor.user.id,
            now=ctx.now,
        )
    ).unwrap()
    return EventOut.model_validate(event)


# --- slots --------------------------------------------------------------------


@router.post("/{event_id}/slots", status_code=201)
async def add_slot(ctx: CtxDep, event_id: int, data: EventSlotIn) -> EventSlotOut:
    slot = (
        await event_service.add_slot(
            ctx.session,
            ctx.actor,
            event_id,
            name=data.name,
            capacity=data.capacity,
            position=data.position,
            description=data.description,
            now=ctx.now,
        )
    ).unwrap()
    return EventSlotOut.model_validate(slot)


@router.patch("/{event_id}/slots/{slot_id}")
async def update_slot(
    ctx: CtxDep, event_id: int, slot_id: int, data: EventSlotPatch
) -> EventSlotOut:
    await _slot_of(ctx, event_id, slot_id)
    slot = (
        await event_service.update_slot(
            ctx.session,
            ctx.actor,
            slot_id,
            **data.model_dump(exclude_unset=True),
            now=ctx.now,
        )
    ).unwrap()
    return EventSlotOut.model_validate(slot)


@router.delete("/{event_id}/slots/{slot_id}", status_code=204)
async def delete_slot(ctx: CtxDep, event_id: int, slot_id: int) -> None:
    await _slot_of(ctx, event_id, slot_id)
    (
        await event_service.delete_slot(ctx.session, ctx.actor, slot_id, now=ctx.now)
    ).unwrap()


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
    (
        await event_service.set_rsvp(
            ctx.session,
            ctx.actor,
            event_id=event_id,
            volunteer_id=ctx.actor.volunteer_id,
            available=data.available,
            note=data.note,
            now=ctx.now,
        )
    ).unwrap()


@router.post("/{event_id}/slots/{slot_id}/assignments", status_code=201)
async def create_assignment(
    ctx: CtxDep, event_id: int, slot_id: int, data: EventAssignIn
) -> EventAssignmentOut:
    """No volunteer_id: sign yourself up. With one: a manager schedules
    that team member."""
    await _slot_of(ctx, event_id, slot_id)
    if data.volunteer_id is None:
        if data.repeat_series:
            assignment, _ = (
                await event_service.sign_up_series(
                    ctx.session,
                    ctx.actor,
                    slot_id=slot_id,
                    volunteer_id=ctx.actor.volunteer_id,
                    notify_7d=data.notify_7d,
                    notify_24h=data.notify_24h,
                    now=ctx.now,
                )
            ).unwrap()
        else:
            assignment = (
                await event_service.sign_up(
                    ctx.session,
                    ctx.actor,
                    slot_id=slot_id,
                    volunteer_id=ctx.actor.volunteer_id,
                    notify_7d=data.notify_7d,
                    notify_24h=data.notify_24h,
                    now=ctx.now,
                )
            ).unwrap()
    else:
        if data.repeat_series:
            raise ValueError("repeat_series applies to self sign-ups only")
        assignment = (
            await event_service.assign(
                ctx.session,
                ctx.actor,
                slot_id=slot_id,
                volunteer_id=data.volunteer_id,
                assigned_by=ctx.actor.user.id,
                now=ctx.now,
            )
        ).unwrap()
    return EventAssignmentOut.model_validate(assignment)


@router.delete("/assignments/{assignment_id}", status_code=204)
async def remove_assignment(ctx: CtxDep, assignment_id: int) -> None:
    """Withdraw yourself, or (as a manager) remove anyone — future events
    only; past rosters are the attendance record."""
    (
        await event_service.remove_assignment(
            ctx.session, ctx.actor, assignment_id, now=ctx.now
        )
    ).unwrap()


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
    sub = (
        await event_service.request_sub(
            ctx.session,
            ctx.actor,
            assignment_id=assignment_id,
            requested_by=ctx.actor.user.id,
            note=data.note,
            now=ctx.now,
        )
    ).unwrap()
    return SubRequestOut.model_validate(sub)


@router.post("/sub-requests/{sub_request_id}/claim")
async def claim_sub(ctx: CtxDep, sub_request_id: int) -> SubRequestOut:
    """First-come claim; the assignment moves to the caller."""
    sub, _assignment, _asker = (
        await event_service.claim_sub(
            ctx.session,
            ctx.actor,
            sub_request_id=sub_request_id,
            volunteer_id=ctx.actor.volunteer_id,
            now=ctx.now,
        )
    ).unwrap()
    return SubRequestOut.model_validate(sub)


@router.post("/sub-requests/{sub_request_id}/cancel")
async def cancel_sub(ctx: CtxDep, sub_request_id: int) -> SubRequestOut:
    sub = await ctx.session.get(EventSubRequest, sub_request_id)
    if sub is None:
        raise LookupError(f"substitute request {sub_request_id} not found")
    sub = (
        await event_service.cancel_sub(
            ctx.session, ctx.actor, sub_request_id, now=ctx.now
        )
    ).unwrap()
    return SubRequestOut.model_validate(sub)


# --- attendance ---------------------------------------------------------------


@router.patch("/assignments/{assignment_id}/attendance")
async def set_attendance(
    ctx: CtxDep, assignment_id: int, data: AttendanceIn
) -> AttendanceRowOut:
    """Record an exception to auto attendance (nulls clear back to auto).
    Past, non-cancelled events only."""
    assignment = (
        await event_service.set_attendance(
            ctx.session,
            ctx.actor,
            assignment_id=assignment_id,
            attended=data.attended,
            hours=Decimal(str(data.hours)) if data.hours is not None else None,
            now=ctx.now,
        )
    ).unwrap()
    event = await _get_or_404(ctx, assignment.event_id)
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


# --- task forces --------------------------------------------------------------
#
# The automated multi-team event. The whole of services/task_force.py was
# unreachable over JSON: an API caller could create an event for one team and
# nothing else, while the GUI could invite other ministries to staff it.


async def _task_force_out(ctx: CtxDep, event_id: int) -> TaskForceOut | None:
    """Build the task-force payload for an event, or None if one team staffs it
    alone. Does NOT authorize: a caller that just performed an authorized
    mutation uses it directly — re-checking here against the actor loaded at
    request start would 403 on a meta team created mid-request. The GET gates
    first, below."""
    view = await task_force_service.get_for_event(ctx.session, event_id)
    if view is None:
        return None
    paths = (await team_service.tree(ctx.session)).paths
    return TaskForceOut(
        event_id=event_id,
        team_id=view.team_id,
        owner_team_id=view.owner_team_id,
        sources=[
            TeamWithPath(**TeamOut.model_validate(t).model_dump(), path=paths[t.id])
            for t in view.sources
        ],
    )


@router.get("/{event_id}/task-force")
async def get_task_force(ctx: CtxDep, event_id: int) -> TaskForceOut | None:
    """The task force behind this event, or null if one team staffs it alone."""
    (
        await event_service.visible(ctx.session, ctx.actor, event_id)
    ).unwrap()  # authorizes
    return await _task_force_out(ctx, event_id)


@router.post("/{event_id}/collaborators", status_code=201)
async def add_collaborator(
    ctx: CtxDep, event_id: int, data: CollaboratorIn
) -> TaskForceOut:
    """Invite another team to staff this event.

    The first one creates the task-force team and repoints the event at it;
    later ones add a source and copy its roster in. Takes manage rights on the
    event, deliberately not on the team being invited — and gives none over that
    team's people, which is what makes asking safe (permissions.Actor).
    """
    (
        await task_force_service.add_collaborating_team(
            ctx.session,
            ctx.actor,
            event_id=event_id,
            source_team_id=data.team_id,
            created_by=ctx.actor.user.id,
            now=ctx.now,
            tz=ctx.env.tz,
        )
    ).unwrap()
    audit_log(
        "event.collaboration_added",
        event_id=event_id,
        source_team_id=data.team_id,
        via="api",
    )
    # already authorized by add_collaborating_team; build the payload without
    # re-checking, so the first collaborator (which creates the meta team the
    # request-start actor cannot yet see) does not 403 and roll back
    return await _task_force_out(ctx, event_id)  # type: ignore[return-value]


@router.post("/{event_id}/task-force/refresh")
async def refresh_task_force(ctx: CtxDep, event_id: int) -> TaskForceOut:
    """Re-copy the source rosters, picking up anyone added to them since.

    Additive: the strongest role a person holds across the sources wins, an
    existing role is never downgraded, and nobody is removed — leaving a task
    force is roster management on the meta team itself."""
    (
        await task_force_service.refresh_rosters(ctx.session, ctx.actor, event_id)
    ).unwrap()
    return await _task_force_out(ctx, event_id)  # type: ignore[return-value]


@router.post("/assignments/{assignment_id}/substitute")
async def substitute(
    ctx: CtxDep, assignment_id: int, data: SubstituteIn
) -> EventAssignmentOut:
    """Hand this slot straight to a named teammate — the claim flow without the
    open call, for when the assignee has already found their own cover.

    Their own slot, or any slot on an event the caller manages. The incoming
    volunteer must really be a member of the event's team, admin or not. Any
    open substitution request on the assignment is cancelled with it.
    """
    assignment, _outgoing, _incoming = (
        await event_service.substitute(
            ctx.session,
            ctx.actor,
            assignment_id=assignment_id,
            new_volunteer_id=data.volunteer_id,
            acted_by=ctx.actor.user.id,
            now=ctx.now,
            notify=ctx.env.notify,
        )
    ).unwrap()
    audit_log(
        "event.slot_handed_over",
        assignment_id=assignment_id,
        to_volunteer_id=data.volunteer_id,
        via="api",
    )
    return EventAssignmentOut.model_validate(assignment)
