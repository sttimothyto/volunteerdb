"""Roster membership: who serves on which team, in what role.

Authorization lives HERE, not at the two front doors. Both the GUI and the
JSON API reach these functions with the actor they loaded, and the check runs
once, in the one place both of them pass through — which is the invariant
docs/explanation/architecture.md describes and the reason a permission cannot
be enforced on one surface and forgotten on the other.

`SYSTEM` (permissions.SYSTEM) is the trusted internal caller: the nightly jobs,
the seed script, and the roster sync, which have no signed-in user to speak for
and are already bounded by what they were asked to do. It is spelled out at
every such call site, so skipping the check is always visible in the diff.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import DomainError, Forbidden, not_found, require
from ..fp import UNSET, Err, Ok, Result
from ..models import Membership, TeamRole
from ..permissions import Actor


def _may_manage(actor: Actor, team_id: int) -> Err[Forbidden] | None:
    return require(actor.can_manage_team(team_id), "manage this team's roster")


async def get(session: AsyncSession, membership_id: int) -> Membership | None:
    return await session.get(Membership, membership_id)


async def get_managed(
    session: AsyncSession, actor: Actor, membership_id: int
) -> Result[Membership, DomainError]:
    """The membership, for a caller entitled to manage its team.

    A plain `get` answers anybody, so a read that is part of managing a roster
    asks for it here — otherwise a no-op PATCH would report which volunteer
    holds which role on which team to a caller with no rights over it."""
    membership = await get(session, membership_id)
    if membership is None:
        return not_found("membership", membership_id)
    if denied := _may_manage(actor, membership.team_id):
        return denied
    return Ok(membership)


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
    actor: Actor,
    volunteer_id: int,
    team_id: int,
    role: TeamRole,
    *,
    existing: Membership | None | object = UNSET,
) -> Result[Membership, DomainError]:
    """Add the volunteer to the team, or update their role if already on it.

    `existing` lets bulk callers (the importer) pass a preloaded membership
    — or None — and skip the per-row lookup."""
    if denied := _may_manage(actor, team_id):
        return denied
    membership = (
        await find(session, volunteer_id, team_id) if existing is UNSET else existing
    )
    # `is None` in spirit: `existing` is annotated `| object` only to carry the
    # UNSET sentinel, so the resolved value is statically wider than the
    # Membership | None it actually is. Spelling the test as isinstance narrows
    # it back, which is what lets the no-op branch below read `.role`.
    if not isinstance(membership, Membership):
        membership = Membership(
            volunteer_id=volunteer_id,
            team_id=team_id,
            role=role,
        )
        session.add(membership)
    elif membership.role == role:
        # Already on the team in this role: nothing to write. Assigning the
        # attribute anyway would mark the row dirty, and the flush below would
        # then walk the whole unit of work to discover there was no change --
        # which is what a re-import mostly is. A roster sheet is re-imported
        # nightly and almost every row is unchanged, so this early return is
        # the difference between a flush per row and a flush per real edit.
        return Ok(membership)
    else:
        membership.role = role
    await session.flush()
    return Ok(membership)


async def set_role(
    session: AsyncSession, actor: Actor, membership_id: int, role: TeamRole
) -> Result[Membership, DomainError]:
    """Change the role somebody holds on a team.

    Exists because both front doors used to reach in and set the column
    themselves — the API with `setattr` on the ORM object, the GUI from a select
    handler — which meant the operation had no single place to authorize, and
    any rule added later would have had to be added twice."""
    managed = await get_managed(session, actor, membership_id)
    if isinstance(managed, Err):
        return managed
    membership = managed.value
    membership.role = role
    await session.flush()
    return Ok(membership)


async def remove(
    session: AsyncSession, actor: Actor, membership_id: int
) -> Result[None, DomainError]:
    managed = await get_managed(session, actor, membership_id)
    if isinstance(managed, Err):
        return managed
    await session.delete(managed.value)
    await session.flush()
    return Ok(None)
