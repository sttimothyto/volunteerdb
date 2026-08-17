from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity, fetch
from ..models import EventTaskForce, Membership, Team, TeamRole, Volunteer


class CycleError(ValueError):
    pass


_UNSET: object = object()


async def get(
    session: AsyncSession, team_id: int, at: datetime | None = None
) -> Team | None:
    T = entity(Team, at)
    rows = await fetch(session, sa.select(T).where(T.id == team_id), at)
    return rows[0][0] if rows else None


async def list_all(session: AsyncSession, at: datetime | None = None) -> list[Team]:
    T = entity(Team, at)
    return [row[0] for row in await fetch(session, sa.select(T).order_by(T.name), at)]


def children_map(teams: list[Team]) -> dict[int | None, list[Team]]:
    by_parent: dict[int | None, list[Team]] = {}
    for t in teams:
        by_parent.setdefault(t.parent_team_id, []).append(t)
    for siblings in by_parent.values():
        siblings.sort(key=lambda t: t.name.lower())
    return by_parent


async def search(
    session: AsyncSession, query: str, at: datetime | None = None
) -> list[tuple[Team, str]]:
    """Active teams whose name, description, or display path contains `query`
    (case-insensitive), as (team, path) sorted by path. Done in Python over
    list_all: paths are computed recursively, and the whole table is ~60 rows.
    No actor: the team directory is visible to every signed-in user."""
    q = query.strip().lower()
    if not q:
        return []
    all_teams = await list_all(session, at)
    paths = team_paths(all_teams)
    hits = [
        (t, paths[t.id])
        for t in all_teams
        if t.is_active
        and (
            q in t.name.lower()
            or q in (t.description or "").lower()
            or q in paths[t.id].lower()
        )
    ]
    hits.sort(key=lambda pair: pair[1].lower())
    return hits


def descendant_ids(teams: list[Team], root_id: int) -> set[int]:
    """root_id plus all transitive sub-team ids."""
    by_parent = children_map(teams)
    result: set[int] = set()
    stack = [root_id]
    while stack:
        tid = stack.pop()
        if tid in result:
            continue
        result.add(tid)
        stack.extend(t.id for t in by_parent.get(tid, []))
    return result


def team_paths(teams: list[Team]) -> dict[int, str]:
    """id -> 'Parent / Child' display path.

    Cycles cannot occur in live data (_check_no_cycle) but this also runs on
    as-of snapshots that nothing validates, so a cycle truncates the path
    instead of recursing until the stack blows.
    """
    by_id = {t.id: t for t in teams}
    paths: dict[int, str] = {}

    def path(t: Team, seen: frozenset[int]) -> str:
        if t.id in paths:
            return paths[t.id]
        parent = by_id.get(t.parent_team_id) if t.parent_team_id else None
        if parent is not None and parent.id not in seen:
            p = f"{path(parent, seen | {t.id})} / {t.name}"
        else:
            p = t.name
        paths[t.id] = p
        return p

    for t in teams:
        path(t, frozenset())
    return paths


def _check_workload_weight(workload_weight: Decimal | None) -> None:
    """Shared by create and update: they disagreed once, and a negative weight
    silently corrupts every workload band downstream."""
    if workload_weight is not None and workload_weight < 0:
        raise ValueError("workload weight must not be negative")


async def create(
    session: AsyncSession,
    name: str,
    parent_team_id: int | None = None,
    description: str | None = None,
    workload_weight: Decimal | None = None,
) -> Team:
    _check_workload_weight(workload_weight)
    team = Team(
        name=name.strip(),
        parent_team_id=parent_team_id,
        description=description,
        workload_weight=workload_weight,
    )
    session.add(team)
    await session.flush()
    return team


async def update(
    session: AsyncSession,
    team_id: int,
    *,
    name: str | None = None,
    parent_team_id: int | None | object = _UNSET,
    description: str | None | object = _UNSET,
    is_active: bool | None = None,
    workload_weight: Decimal | None | object = _UNSET,
) -> Team:
    team = await get(session, team_id)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    if name is not None:
        team.name = name.strip()
    if parent_team_id is not _UNSET:
        await _check_no_cycle(session, team_id, parent_team_id)  # type: ignore[arg-type]
        team.parent_team_id = parent_team_id  # type: ignore[assignment]
    if description is not _UNSET:
        team.description = description  # type: ignore[assignment]
    if is_active is not None:
        team.is_active = is_active
    if workload_weight is not _UNSET:
        _check_workload_weight(workload_weight)  # type: ignore[arg-type]
        team.workload_weight = workload_weight  # type: ignore[assignment]
    await session.flush()
    return team


async def _check_no_cycle(
    session: AsyncSession, team_id: int, new_parent_id: int | None
) -> None:
    if new_parent_id is None:
        return
    teams = await list_all(session)
    if new_parent_id == team_id or new_parent_id in descendant_ids(teams, team_id):
        raise CycleError("a team cannot be its own ancestor")


# the form URL is mailed verbatim to whatever address a public-form submitter
# typed, so only Google Forms links are accepted — never an arbitrary URL
GOOGLE_FORM_PREFIXES = ("https://docs.google.com/forms/", "https://forms.gle/")


async def set_application_form_url(
    session: AsyncSession, team_id: int, url: str | None
) -> Team:
    """Set or clear the team's Google application form; validates the link shape."""
    team = await get(session, team_id)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    if url and url.strip():
        cleaned = url.strip()
        if not cleaned.startswith(GOOGLE_FORM_PREFIXES):
            raise ValueError(
                "not a Google Form link — expected https://docs.google.com/forms/… "
                "or https://forms.gle/…"
            )
        team.application_form_url = cleaned
    else:
        team.application_form_url = None
    await session.flush()
    return team


async def delete(session: AsyncSession, team_id: int) -> None:
    team = await get(session, team_id)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    # a live task force is deleted by its event's teardown, never directly:
    # the event still points at this team, and event.team_id CASCADEs — a
    # direct delete would take the event and its attendance record with it
    live = await session.scalar(
        sa.select(EventTaskForce.event_id).where(EventTaskForce.team_id == team_id)
    )
    if live is not None:
        raise ValueError(
            f"this team is the task force of event {live} — it is removed "
            "automatically after the event ends (manage it from the event page)"
        )
    await session.delete(team)
    await session.flush()


async def roster(
    session: AsyncSession, team_id: int, at: datetime | None = None
) -> list[tuple[Membership, Volunteer]]:
    """Memberships with their volunteers, leaders first."""
    M, V = entity(Membership, at), entity(Volunteer, at)
    role_order = sa.case(
        {role.value: i for i, role in enumerate(TeamRole)},
        value=sa.cast(M.role, sa.String),
    )
    stmt = (
        sa.select(M, V)
        .join(V, V.id == M.volunteer_id)
        .where(M.team_id == team_id)
        .order_by(role_order, V.last_name, V.first_name)
    )
    return [(m, v) for m, v in await fetch(session, stmt, at)]
