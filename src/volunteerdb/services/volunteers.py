from dataclasses import dataclass
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity, fetch
from ..models import (
    Membership,
    Team,
    TeamRole,
    Volunteer,
    membership_history,
    team_history,
)
from ..permissions import Actor

_UNSET: object = object()


async def get(
    session: AsyncSession, volunteer_id: int, at: datetime | None = None
) -> Volunteer | None:
    V = entity(Volunteer, at)
    rows = await fetch(session, sa.select(V).where(V.id == volunteer_id), at)
    return rows[0][0] if rows else None


async def search(
    session: AsyncSession,
    query: str = "",
    at: datetime | None = None,
    include_inactive: bool = False,
    *,
    actor: Actor | None = None,
    limit: int | None = None,
) -> list[Volunteer]:
    """Substring search over every volunteer column.

    Name and email match for everyone. (The rev-0005 trigram indexes cover
    only those two predicates; since the private branches below joined the
    same OR, every non-blank search seq-scans — sub-millisecond at parish
    scale, so the indexes are retained as harmless until search is
    restructured.) Phone, notes, and custom-field values additionally match —
    but, unless the actor is an admin (or None: trusted internal caller), only
    among volunteers the actor can already view unredacted, mirroring
    can_view_volunteer: self, plus anyone on a team in full_view_team_ids.
    Matching a private field a viewer can't see would leak its content by the
    row's mere presence.

    `limit` caps the rows the database returns (for the search-box typeahead);
    since the order is by name, that is the alphabetically first N matches.
    """
    V = entity(Volunteer, at)
    stmt = sa.select(V).order_by(V.last_name, V.first_name)
    if query.strip():
        pattern = f"%{query.strip()}%"
        public_match = sa.or_(
            (V.first_name + " " + V.last_name).ilike(pattern),
            V.email.ilike(pattern),
        )
        private_match = sa.or_(
            V.phone.ilike(pattern),
            V.notes.ilike(pattern),
            # serialized JSONB: matches values (and keys) as text
            sa.cast(V.custom, sa.Text).ilike(pattern),
        )
        if actor is None or actor.is_admin:
            stmt = stmt.where(sa.or_(public_match, private_match))
        else:
            M = entity(Membership, at)
            visible_preds = []
            if actor.volunteer_id is not None:
                visible_preds.append(V.id == actor.volunteer_id)
            if actor.full_view_team_ids:
                visible_preds.append(
                    sa.exists(
                        sa.select(sa.literal(1)).where(
                            M.volunteer_id == V.id,
                            M.team_id.in_(actor.full_view_team_ids),
                        )
                    )
                )
            visible = sa.or_(*visible_preds) if visible_preds else sa.false()
            stmt = stmt.where(sa.or_(public_match, sa.and_(private_match, visible)))
    if not include_inactive:
        stmt = stmt.where(V.is_active)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [row[0] for row in await fetch(session, stmt, at)]


async def name_map(
    session: AsyncSession, include_inactive: bool = False
) -> dict[int, str]:
    """id → 'First Last' for selection widgets, in list order — three columns
    instead of full entities (notes and the custom JSONB stay in the
    database, and out of the serialized page)."""
    stmt = sa.select(Volunteer.id, Volunteer.first_name, Volunteer.last_name).order_by(
        Volunteer.last_name, Volunteer.first_name
    )
    if not include_inactive:
        stmt = stmt.where(Volunteer.is_active)
    rows = await session.execute(stmt)
    return {vid: f"{first} {last}" for vid, first, last in rows}


async def find_by_email(session: AsyncSession, email: str) -> list[Volunteer]:
    """Active volunteers holding exactly this address, oldest first.

    A list, not a row: volunteer.email is deliberately not unique — families
    share an address — so the caller decides what an ambiguous match means.
    lower() on the column as well as the argument, since rows written before
    create/update normalized (or written by SQL) may be mixed case; that skips
    ix_volunteer_email, which at parish scale costs nothing.
    """
    addr = email.strip().lower()
    if not addr:
        return []
    return list(
        (
            await session.execute(
                sa.select(Volunteer)
                .where(Volunteer.is_active, sa.func.lower(Volunteer.email) == addr)
                .order_by(Volunteer.id)
            )
        ).scalars()
    )


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
        sa.select(M, T)
        .join(T, T.id == M.team_id)
        .where(M.volunteer_id == volunteer_id)
        .order_by(T.name)
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
    if not rows:
        return []
    # one grouped count over all their teams; scalar rows, so no fetch() needed
    M = entity(Membership, at)
    counts_stmt = (
        sa.select(M.team_id, M.role, sa.func.count())
        .where(
            M.team_id.in_([team.id for _, team in rows]), M.volunteer_id != volunteer_id
        )
        .group_by(M.team_id, M.role)
    )
    counts = {
        (team_id, role): n for team_id, role, n in await session.execute(counts_stmt)
    }
    result: list[ImpactRow] = []
    for membership, team in rows:
        leaders = counts.get((team.id, TeamRole.leader), 0)
        seconds = counts.get((team.id, TeamRole.second), 0)
        result.append(
            ImpactRow(
                team=team,
                role=membership.role,
                leaders_left=leaders,
                leadership_left=leaders + seconds,
            )
        )
    # most critical first: the fewer leaders remain, the higher it sorts
    result.sort(key=lambda r: (r.leadership_left, r.leaders_left, r.team.name.lower()))
    return result


@dataclass
class RoleSegment:
    role: TeamRole
    start: datetime  # tz-aware
    end: datetime | None  # None = ongoing


@dataclass
class MembershipSpell:
    team_id: int
    team_name: str  # current name; latest historical name if the team is gone
    team_deleted: bool
    role: TeamRole  # final role held in this spell
    start: date  # when the membership record was created
    end: date | None  # when the membership record was deleted; None = ongoing
    segments: list[RoleSegment]  # consecutive role stretches within the spell


async def timeline(session: AsyncSession, volunteer_id: int) -> list[MembershipSpell]:
    """Membership spells over all time, stitched from the audit trail.

    A spell is one continuous stretch on a team. Row versions of a single
    membership abut exactly (the trigger closes and reopens sys_period at the
    same instant), so a gap between versions of the same (volunteer, team)
    means leave-then-rejoin under a fresh membership id — a new spell. Both
    ends are system times: they trail the real-world join/leave by however
    long the operator waited (rev 0011 dropped the operator-entered
    joined_on). A delete recreated in the same instant (e.g. by an importer)
    shows as two abutting spells.
    """
    mh = membership_history
    cols = ("id", "team_id", "role", "sys_period")
    hist = sa.select(*[mh.c[n] for n in cols], mh.c.op).where(
        mh.c.volunteer_id == volunteer_id
    )
    live = sa.select(
        *[Membership.__table__.c[n] for n in cols],
        sa.literal(None, sa.CHAR(1)).label("op"),
    ).where(Membership.volunteer_id == volunteer_id)
    versions = (await session.execute(hist.union_all(live))).all()

    runs: list[list[sa.Row]] = []
    prev_key: tuple[int, datetime | None] | None = None
    for row in sorted(versions, key=lambda r: (r.team_id, r.sys_period.lower)):
        prev_team, prev_upper = prev_key or (None, None)
        if (
            row.team_id != prev_team
            or prev_upper is None
            or row.sys_period.lower > prev_upper
        ):
            runs.append([])
        runs[-1].append(row)
        prev_key = (row.team_id, row.sys_period.upper)

    team_ids = {run[0].team_id for run in runs}
    names: dict[int, str] = {}
    if team_ids:
        stmt = sa.select(Team.id, Team.name).where(Team.id.in_(team_ids))
        names = dict((await session.execute(stmt)).all())  # type: ignore[arg-type]
    gone = team_ids - names.keys()
    if gone:
        stmt = (
            sa.select(team_history.c.id, team_history.c.name)
            .where(team_history.c.id.in_(gone))
            .order_by(team_history.c.id, team_history.c.sys_period.desc())
            .distinct(team_history.c.id)
        )
        names |= dict((await session.execute(stmt)).all())  # type: ignore[arg-type]

    spells = []
    for run in runs:
        first, last = run[0], run[-1]
        ended = last.op == "D"
        start = first.sys_period.lower.astimezone().date()
        segments: list[RoleSegment] = []
        for row in run:
            if segments and segments[-1].role == row.role:
                # same-role consecutive versions: pre-0011 history rows where
                # only joined_on/notes changed
                segments[-1].end = row.sys_period.upper
            else:
                segments.append(
                    RoleSegment(row.role, row.sys_period.lower, row.sys_period.upper)
                )
        spells.append(
            MembershipSpell(
                team_id=first.team_id,
                team_name=names.get(first.team_id, "unknown team"),
                team_deleted=first.team_id in gone,
                role=last.role,
                start=start,
                end=last.sys_period.upper.astimezone().date() if ended else None,
                segments=segments,
            )
        )
    spells.sort(key=lambda s: (s.start, s.team_name.lower()))
    return spells
