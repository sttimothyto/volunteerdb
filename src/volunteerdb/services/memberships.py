import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Membership, TeamRole

_UNSET: object = object()


async def get(session: AsyncSession, membership_id: int) -> Membership | None:
    return await session.get(Membership, membership_id)


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
    volunteer_id: int,
    team_id: int,
    role: TeamRole,
    *,
    existing: Membership | None | object = _UNSET,
) -> Membership:
    """Add the volunteer to the team, or update their role if already on it.

    `existing` lets bulk callers (the importer) pass a preloaded membership
    — or None — and skip the per-row lookup."""
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


async def remove(session: AsyncSession, membership_id: int) -> None:
    membership = await get(session, membership_id)
    if membership is None:
        raise LookupError(f"membership {membership_id} not found")
    await session.delete(membership)
    await session.flush()
