"""Task forces: the automated multi-team event.

The events doc's rule is "one event, one team — a parish-wide occasion gets
its own task-force team first". This service builds that team for you:
adding a collaborating team to an event creates (once) a meta team as a
CHILD of the owning team, copies in the union of the source rosters, and
repoints the event. The child placement keeps permissions boring — the
owner's leaders manage the meta team through the ordinary subtree cascade —
and collaborating leaders co-manage because their leader/second roles copy
in: collaboration means co-management, a deliberate choice.

Rosters copy additively (highest role wins per person, never downgraded,
nobody removed); drift in a source team is picked up by the explicit
refresh, not a background sync. Teardown, once the event ended or was
cancelled, restores event.team_id to the owner and hard-deletes the meta
team — its memberships stay visible in as-of history via the ordinary team
history twins, which is the "visible in history" the feature promises.
THE TEARDOWN ORDER IS LOAD-BEARING: event.team_id cascades on team delete,
so the repoint must flush before the delete or the event (and its whole
attendance record) dies with the team.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import DomainError, Invalid, invalid, not_found, require
from ..fp import Err, Ok, Result
from ..models import (
    Event,
    EventStatus,
    EventTaskForceSource,
    Membership,
    Team,
    TeamRole,
)
from ..permissions import Actor
from . import mail, memberships
from . import teams as team_service

# leader outranks second outranks core outranks member — the copy keeps the
# strongest role a person holds across the source teams
ROLE_RANK: dict[TeamRole, int] = {role: i for i, role in enumerate(TeamRole)}

TEAM_NAME_MAX = 200


@dataclass(frozen=True)
class TaskForceView:
    """An event's task force: the meta team it was repointed to, the owner it
    will be given back at teardown, and the teams that contributed rosters."""

    event: Event
    team_id: int  # the meta team; membership of it gates sign-up
    owner_team_id: int | None  # None only if the owner team was deleted
    sources: list[Team]


def _require_upcoming(event: Event, action: str, now: datetime) -> Err[Invalid] | None:
    if event.status != EventStatus.scheduled.value:
        return invalid(f"cannot {action}: event is cancelled")
    if event.ends_at <= now:
        return invalid(f"cannot {action}: event has already ended")
    return None


async def get_for_event(session: AsyncSession, event_id: int) -> TaskForceView | None:
    """The event's task force, or None when one team staffs it alone."""
    event = await session.get(Event, event_id)
    if event is None or event.task_force_team_id is None:
        return None
    sources = list(
        await session.scalars(
            sa.select(Team)
            .join(EventTaskForceSource, EventTaskForceSource.team_id == Team.id)
            .where(EventTaskForceSource.event_id == event_id)
            .order_by(Team.name)
        )
    )
    return TaskForceView(
        event=event,
        team_id=event.task_force_team_id,
        owner_team_id=event.owner_team_id,
        sources=sources,
    )


async def _create_meta_team(
    session: AsyncSession, event: Event, owner_team_id: int, tz: ZoneInfo
) -> Result[Team, DomainError]:
    day = event.starts_at.astimezone(tz).date()
    base = f"{event.title} ({day}) task force"[:TEAM_NAME_MAX]
    name = base
    taken = await session.scalar(
        sa.select(Team.id).where(
            Team.parent_team_id == owner_team_id, Team.name == name
        )
    )
    if taken is not None:  # same title, same day, same parent — rare but real
        suffix = f" #{event.id}"
        name = base[: TEAM_NAME_MAX - len(suffix)] + suffix
    where = f" at {event.location}" if event.location else ""
    description = (
        f"[Auto] Task force for the event “{event.title}” on "
        f"{mail.event_when(event.starts_at, event.ends_at, tz=tz)}{where}. Managed "
        "from the event page; removed automatically after the event ends."
    )
    return await team_service.create(
        # None: the caller already holds manage rights on the event whose task
        # force this is, and the meta team exists only to carry its roster
        session,
        None,
        name,
        parent_team_id=owner_team_id,
        description=description,
    )


async def add_collaborating_team(
    session: AsyncSession,
    actor: Actor | None,
    *,
    event_id: int,
    source_team_id: int,
    created_by: int | None,
    now: datetime,
    tz: ZoneInfo,
) -> Result[Team, DomainError]:
    """First collaborator: create the meta team, seed sources with the owner
    and the newcomer, copy both rosters, repoint the event. Later ones: add
    the source and copy its roster. Returns the meta team.

    Manage rights on the event's own team — and deliberately NOT on the source:
    inviting another ministry to staff your event is a request you make of the
    parish, not a power over that team. What keeps it safe is the other half,
    in permissions.Actor: the meta team confers rights over the EVENT and never
    over the people it borrowed, so a collaboration cannot hand you anybody's
    contact details, notes, workload or invite link.

    This function authorized nothing at all before, and the checks sat at the
    two front doors — which is how the GUI came to check the wrong team.
    """
    event = await session.get(Event, event_id)
    if event is None:
        return not_found("event", event_id)
    if denied := require(
        actor is None or actor.can_manage_team(event.team_id),
        "manage this event",
    ):
        return denied
    if closed := _require_upcoming(event, "add a collaborating team", now):
        return closed
    if await session.get(Team, source_team_id) is None:
        return not_found("team", source_team_id)

    if event.task_force_team_id is None:
        if source_team_id == event.team_id:
            return invalid("that team already staffs this event")
        owner_team_id = event.team_id
        made = await _create_meta_team(session, event, owner_team_id, tz)
        if isinstance(made, Err):
            return made
        meta = made.value
        session.add(EventTaskForceSource(event_id=event.id, team_id=owner_team_id))
        session.add(EventTaskForceSource(event_id=event.id, team_id=source_team_id))
        event.owner_team_id = owner_team_id
        event.task_force_team_id = meta.id
        event.team_id = meta.id  # membership of the meta team now gates sign-up
        await session.flush()
    else:
        if source_team_id == event.task_force_team_id:
            return invalid("that is the task force itself")
        already = await session.scalar(
            sa.select(sa.literal(1))
            .select_from(EventTaskForceSource)
            .where(
                EventTaskForceSource.event_id == event_id,
                EventTaskForceSource.team_id == source_team_id,
            )
        )
        if already is not None:
            return invalid("that team already staffs this event")
        session.add(EventTaskForceSource(event_id=event_id, team_id=source_team_id))
        await session.flush()
    refreshed = await refresh_rosters(session, None, event_id)  # authorized above
    if isinstance(refreshed, Err):
        return refreshed
    meta_team = await session.get(Team, event.task_force_team_id)  # type: ignore[arg-type]
    assert meta_team is not None  # just created or repointed to
    return Ok(meta_team)


async def refresh_rosters(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> Result[int, DomainError]:
    """Copy the union of the source rosters into the meta team, additively:
    highest role wins per person, an existing meta role is never downgraded,
    nobody is removed (leaving the task force is roster management on the
    meta team itself). Returns how many people were added."""
    event = await session.get(Event, event_id)
    if event is None or event.task_force_team_id is None:
        return not_found("task force for event", event_id)
    meta_team_id = event.task_force_team_id
    if denied := require(
        actor is None or actor.can_manage_team(meta_team_id),
        "manage this event",
    ):
        return denied
    source_ids = list(
        await session.scalars(
            sa.select(EventTaskForceSource.team_id).where(
                EventTaskForceSource.event_id == event_id
            )
        )
    )
    wanted: dict[int, TeamRole] = {}
    for m in await session.scalars(
        sa.select(Membership).where(Membership.team_id.in_(source_ids))
    ):
        held = wanted.get(m.volunteer_id)
        if held is None or ROLE_RANK[m.role] < ROLE_RANK[held]:
            wanted[m.volunteer_id] = m.role
    existing = {
        m.volunteer_id: m
        for m in await session.scalars(
            sa.select(Membership).where(Membership.team_id == meta_team_id)
        )
    }
    added = 0
    for volunteer_id, role in wanted.items():
        current = existing.get(volunteer_id)
        if current is None:
            put = await memberships.assign(
                session, None, volunteer_id, meta_team_id, role, existing=None
            )
            if isinstance(put, Err):
                return put
            added += 1
        elif ROLE_RANK[role] < ROLE_RANK[current.role]:
            put = await memberships.assign(
                session, None, volunteer_id, meta_team_id, role, existing=current
            )
            if isinstance(put, Err):
                return put
    return Ok(added)


async def teardown_due(session: AsyncSession, *, now: datetime) -> list[int]:
    """Event ids whose task force should be dismantled: the event ended, or
    was cancelled."""
    return list(
        await session.scalars(
            sa.select(Event.id).where(
                Event.task_force_team_id.is_not(None),
                sa.or_(
                    Event.ends_at <= now,
                    Event.status == EventStatus.cancelled,
                ),
            )
        )
    )


async def teardown(session: AsyncSession, event_id: int) -> Result[None, DomainError]:
    """Restore the owner, then dismantle the meta team. Assignments and
    RSVPs reference the volunteer and the event, never the team, so the
    attendance record survives intact."""
    event = await session.get(Event, event_id)
    if event is None or event.task_force_team_id is None:
        return not_found("task force for event", event_id)
    meta_team_id = event.task_force_team_id
    if event.owner_team_id is None:
        # The owner team was deleted while the task force was live, so there is
        # nothing to give the event back to. Keep it where it is: the meta team
        # stops being a task force and becomes the team that staffs this event,
        # which is a strange-looking but intact record. The alternative is
        # deleting the meta team out from under the event and losing its whole
        # attendance history with it.
        event.task_force_team_id = None
        await session.flush()
        return Ok(None)
    # repoint FIRST, then let go of the meta team. The columns are ON DELETE SET
    # NULL now (models.Event), so this is no longer the load-bearing ordering it
    # once was — but the event still has to point somewhere real before the team
    # it points at goes away, because event.team_id itself cascades.
    event.team_id = event.owner_team_id
    event.task_force_team_id = None
    event.owner_team_id = None
    await session.flush()
    await session.execute(
        sa.delete(EventTaskForceSource).where(EventTaskForceSource.event_id == event_id)
    )
    await session.flush()
    return await team_service.delete(
        session, None, meta_team_id
    )  # teardown, not a user act
