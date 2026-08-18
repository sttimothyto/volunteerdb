"""Roster membership: who serves on which team, in what role.

Authorization lives HERE, not at the two front doors. Both the GUI and the
JSON API reach these functions with the actor they loaded, and the check runs
once, in the one place both of them pass through — which is the invariant
docs/explanation/architecture.md describes and the reason a permission cannot
be enforced on one surface and forgotten on the other.

`actor=None` means a trusted internal caller: the nightly jobs, the seed
script, and the roster sync, which have no signed-in user to speak for and are
already bounded by what they were asked to do. It is spelled out at every such
call site rather than defaulted, so skipping the check is always visible.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Membership, TeamRole
from ..permissions import Actor, require

_UNSET: object = object()


def _may_manage(actor: Actor | None, team_id: int) -> None:
    require(
        actor is None or actor.can_manage_team(team_id), "manage this team's roster"
    )


async def get(session: AsyncSession, membership_id: int) -> Membership | None:
    return await session.get(Membership, membership_id)


async def get_managed(
    session: AsyncSession, actor: Actor | None, membership_id: int
) -> Membership:
    """The membership, for a caller entitled to manage its team.

    A plain `get` answers anybody, so a read that is part of managing a roster
    asks for it here — otherwise a no-op PATCH would report which volunteer
    holds which role on which team to a caller with no rights over it."""
    membership = await get(session, membership_id)
    if membership is None:
        raise LookupError(f"membership {membership_id} not found")
    _may_manage(actor, membership.team_id)
    return membership


async def find(
    session: AsyncSession, volunteer_id: int, team_id: int
) -> Membership | None:
    return (
        await session.execute(
            sa.select(Membership).where(
                Membership.volunteer_id == volunteer_id, Membership.team_id == team_id
            )
        )
    ).scalar_one_or_none()


async def assign(
    session: AsyncSession,
    actor: Actor | None,
    volunteer_id: int,
    team_id: int,
    role: TeamRole,
    *,
    existing: Membership | None | object = _UNSET,
) -> Membership:
    """Add the volunteer to the team, or update their role if already on it.

    `existing` lets bulk callers (the importer) pass a preloaded membership
    — or None — and skip the per-row lookup."""
    _may_manage(actor, team_id)
    membership = (
        await find(session, volunteer_id, team_id) if existing is _UNSET else existing
    )
    if membership is None:
        membership = Membership(
            volunteer_id=volunteer_id,
            team_id=team_id,
            role=role,
        )
        session.add(membership)
    else:
        membership.role = role
    await session.flush()
    return membership


async def set_role(
    session: AsyncSession, actor: Actor | None, membership_id: int, role: TeamRole
) -> Membership:
    """Change the role somebody holds on a team.

    Exists because both front doors used to reach in and set the column
    themselves — the API with `setattr` on the ORM object, the GUI from a select
    handler — which meant the operation had no single place to authorize, and
    any rule added later would have had to be added twice."""
    membership = await get_managed(session, actor, membership_id)
    membership.role = role
    await session.flush()
    return membership


async def remove(
    session: AsyncSession, actor: Actor | None, membership_id: int
) -> None:
    membership = await get_managed(session, actor, membership_id)
    await session.delete(membership)
    await session.flush()
