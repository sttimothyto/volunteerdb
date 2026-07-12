from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity, fetch
from ..models import Membership, Team, TeamRole, Volunteer

_UNSET: object = object()


async def get(session: AsyncSession, volunteer_id: int, at: datetime | None = None) -> Volunteer | None:
    V = entity(Volunteer, at)
    rows = await fetch(session, sa.select(V).where(V.id == volunteer_id), at)
    return rows[0][0] if rows else None


async def search(
    session: AsyncSession,
    query: str = "",
    at: datetime | None = None,
    include_inactive: bool = False,
) -> list[Volunteer]:
    V = entity(Volunteer, at)
    stmt = sa.select(V).order_by(V.last_name, V.first_name)
    if query.strip():
        pattern = f"%{query.strip()}%"
        stmt = stmt.where(
            sa.or_(
                (V.first_name + " " + V.last_name).ilike(pattern),
                V.email.ilike(pattern),
            )
        )
    if not include_inactive:
        stmt = stmt.where(V.is_active)
    return [row[0] for row in await fetch(session, stmt, at)]


async def create(
    session: AsyncSession,
    first_name: str,
    last_name: str,
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
) -> Volunteer:
    volunteer = Volunteer(
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        email=email.strip().lower() if email else None,
        phone=phone,
        notes=notes,
    )
    session.add(volunteer)
    await session.flush()
    return volunteer


async def update(
    session: AsyncSession,
    volunteer_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None | object = _UNSET,
    phone: str | None | object = _UNSET,
    notes: str | None | object = _UNSET,
    is_active: bool | None = None,
) -> Volunteer:
    volunteer = await get(session, volunteer_id)
    if volunteer is None:
        raise LookupError(f"volunteer {volunteer_id} not found")
    if first_name is not None:
        volunteer.first_name = first_name.strip()
    if last_name is not None:
        volunteer.last_name = last_name.strip()
    if email is not _UNSET:
        volunteer.email = email.strip().lower() if email else None  # type: ignore[union-attr]
    if phone is not _UNSET:
        volunteer.phone = phone  # type: ignore[assignment]
    if notes is not _UNSET:
        volunteer.notes = notes  # type: ignore[assignment]
    if is_active is not None:
        volunteer.is_active = is_active
    await session.flush()
    return volunteer


async def delete(session: AsyncSession, volunteer_id: int) -> None:
    volunteer = await get(session, volunteer_id)
    if volunteer is None:
        raise LookupError(f"volunteer {volunteer_id} not found")
    await session.delete(volunteer)
    await session.flush()


async def assignments(
    session: AsyncSession, volunteer_id: int, at: datetime | None = None
) -> list[tuple[Membership, Team]]:
    """Every (membership, team) this volunteer holds — the cross-reference view."""
    M, T = entity(Membership, at), entity(Team, at)
    stmt = (
        sa.select(M, T).join(T, T.id == M.team_id).where(M.volunteer_id == volunteer_id).order_by(T.name)
    )
    return [(m, t) for m, t in await fetch(session, stmt, at)]


@dataclass
class ImpactRow:
    team: Team
    role: TeamRole
    leaders_left: int  # leaders remaining on the team if this volunteer leaves
    leadership_left: int  # leaders + seconds remaining if this volunteer leaves


async def impact(
    session: AsyncSession, volunteer_id: int, at: datetime | None = None
) -> list[ImpactRow]:
    """The priest's question: if this volunteer leaves, what holes appear?

    For every team the volunteer serves on, how many leaders / leaders+seconds
    would remain. leaders_left == 0 on a team they lead means a leaderless team.
    """
    rows = await assignments(session, volunteer_id, at)
    result: list[ImpactRow] = []
    M = entity(Membership, at)
    for membership, team in rows:
        counts = dict(
            (
                await session.execute(
                    sa.select(M.role, sa.func.count())
                    .where(M.team_id == team.id, M.volunteer_id != volunteer_id)
                    .group_by(M.role)
                )
            ).all()
        )
        leaders = counts.get(TeamRole.leader, 0)
        seconds = counts.get(TeamRole.second, 0)
        result.append(
            ImpactRow(team=team, role=membership.role, leaders_left=leaders, leadership_left=leaders + seconds)
        )
    # most critical first: the fewer leaders remain, the higher it sorts
    result.sort(key=lambda r: (r.leadership_left, r.leaders_left, r.team.name.lower()))
    return result
