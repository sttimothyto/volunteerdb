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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    Event,
    EventStatus,
    EventTaskForce,
    EventTaskForceSource,
    Membership,
    Team,
    TeamRole,
)
from ..permissions import Actor, require
from . import mail, memberships
from . import teams as team_service

# leader outranks second outranks core outranks member — the copy keeps the
# strongest role a person holds across the source teams
ROLE_RANK: dict[TeamRole, int] = {role: i for i, role in enumerate(TeamRole)}

TEAM_NAME_MAX = 200


@dataclass(frozen=True)
class TaskForceView:
    task_force: EventTaskForce
    sources: list[Team]


def _require_upcoming(event: Event, action: str) -> None:
    if event.status != EventStatus.scheduled.value:
        raise ValueError(f"cannot {action}: event is cancelled")
    if event.ends_at <= datetime.now(UTC):
        raise ValueError(f"cannot {action}: event has already ended")


async def get_for_event(session: AsyncSession, event_id: int) -> TaskForceView | None:
    tf = await session.get(EventTaskForce, event_id)
    if tf is None:
        return None
    sources = list(
        await session.scalars(
            sa.select(Team)
            .join(EventTaskForceSource, EventTaskForceSource.team_id == Team.id)
            .where(EventTaskForceSource.event_id == event_id)
            .order_by(Team.name)
        )
    )
    return TaskForceView(task_force=tf, sources=sources)


async def _create_meta_team(
    session: AsyncSession, event: Event, owner_team_id: int
) -> Team:
    tz = ZoneInfo(settings().timezone)
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
        f"{mail.event_when(event.starts_at, event.ends_at)}{where}. Managed "
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
) -> Team:
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
        raise LookupError(f"event {event_id} not found")
    require(
        actor is None or actor.can_manage_team(event.team_id),
        "manage this event",
    )
    _require_upcoming(event, "add a collaborating team")
    if await session.get(Team, source_team_id) is None:
        raise LookupError(f"team {source_team_id} not found")

    tf = await session.get(EventTaskForce, event_id)
    if tf is None:
        if source_team_id == event.team_id:
            raise ValueError("that team already staffs this event")
        owner_team_id = event.team_id
        meta = await _create_meta_team(session, event, owner_team_id)
        tf = EventTaskForce(
            event_id=event.id,
            team_id=meta.id,
            owner_team_id=owner_team_id,
            created_by=created_by,
        )
        session.add(tf)
        session.add(EventTaskForceSource(event_id=event.id, team_id=owner_team_id))
        session.add(EventTaskForceSource(event_id=event.id, team_id=source_team_id))
        await session.flush()
        event.team_id = meta.id  # membership of the meta team now gates sign-up
        await session.flush()
    else:
        if source_team_id == tf.team_id:
            raise ValueError("that is the task force itself")
        already = await session.scalar(
            sa.select(sa.literal(1))
            .select_from(EventTaskForceSource)
            .where(
                EventTaskForceSource.event_id == event_id,
                EventTaskForceSource.team_id == source_team_id,
            )
        )
        if already is not None:
            raise ValueError("that team already staffs this event")
        session.add(EventTaskForceSource(event_id=event_id, team_id=source_team_id))
        await session.flush()
    await refresh_rosters(session, None, event_id)  # authorized above
    return await session.get(Team, tf.team_id)  # type: ignore[return-value]


async def refresh_rosters(
    session: AsyncSession, actor: Actor | None, event_id: int
) -> int:
    """Copy the union of the source rosters into the meta team, additively:
    highest role wins per person, an existing meta role is never downgraded,
    nobody is removed (leaving the task force is roster management on the
    meta team itself). Returns how many people were added."""
    tf = await session.get(EventTaskForce, event_id)
    if tf is None:
        raise LookupError(f"event {event_id} has no task force")
    require(
        actor is None or actor.can_manage_team(tf.team_id),
        "manage this event",
    )
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
            sa.select(Membership).where(Membership.team_id == tf.team_id)
        )
    }
    added = 0
    for volunteer_id, role in wanted.items():
        current = existing.get(volunteer_id)
        if current is None:
            await memberships.assign(
                session, None, volunteer_id, tf.team_id, role, existing=None
            )
            added += 1
        elif ROLE_RANK[role] < ROLE_RANK[current.role]:
            await memberships.assign(
                session, None, volunteer_id, tf.team_id, role, existing=current
            )
    return added


async def teardown_due(session: AsyncSession) -> list[int]:
    """Event ids whose task force should be dismantled: the event ended, or
    was cancelled."""
    return list(
        await session.scalars(
            sa.select(EventTaskForce.event_id)
            .join(Event, Event.id == EventTaskForce.event_id)
            .where(
                sa.or_(
                    Event.ends_at <= sa.func.now(),
                    Event.status == EventStatus.cancelled.value,
                )
            )
        )
    )


async def teardown(session: AsyncSession, event_id: int) -> None:
    """Restore the owner, then dismantle the meta team. Assignments and
    RSVPs reference the volunteer and the event, never the team, so the
    attendance record survives intact."""
    tf = await session.get(EventTaskForce, event_id)
    if tf is None:
        raise LookupError(f"event {event_id} has no task force")
    meta_team_id = tf.team_id
    event = await session.get(Event, event_id)
    if event is not None:
        # repoint and FLUSH before the team delete: event.team_id cascades
        # on team delete, and the reverse order deletes the event itself
        event.team_id = tf.owner_team_id
        await session.flush()
    await session.delete(tf)  # sources cascade; frees the teams.delete guard
    await session.flush()
    await team_service.delete(session, None, meta_team_id)  # teardown, not a user act
