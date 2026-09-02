from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .. import team_cache
from ..errors import DomainError, Invalid, invalid, not_found, require
from ..fp import Err, Ok, Result
from ..history import entity, fetch
from ..models import Event, Membership, Team, TeamRole, TeamSheet, Volunteer
from ..permissions import Actor
from ..sheets.common import extract_spreadsheet_id

_UNSET: object = object()


async def get(
    session: AsyncSession, team_id: int, at: datetime | None = None
) -> Team | None:
    T = entity(Team, at)
    rows = await fetch(session, sa.select(T).where(T.id == team_id), at)
    return rows[0][0] if rows else None


@dataclass(frozen=True, slots=True)
class TeamTree:
    """Every shape of the team tree a caller needs, computed once.

    Read-only: `teams` is a tuple, and the three maps must not be mutated — one
    session shares a single instance between everything that asks for it.
    """

    teams: tuple[Team, ...]
    by_id: dict[int, Team]
    paths: dict[int, str]  # id -> "Parent / Child"
    by_parent: dict[int | None, list[Team]]  # parent id (None = root) -> children

    def descendants(self, root_id: int) -> set[int]:
        """root_id plus all transitive sub-team ids.

        Walks the prebuilt by_parent instead of rebuilding the children map per
        call, which is what load_actor paid once per role the actor holds.
        """
        result: set[int] = set()
        stack = [root_id]
        while stack:
            tid = stack.pop()
            if tid in result:
                continue
            result.add(tid)
            stack.extend(t.id for t in self.by_parent.get(tid, []))
        return result


async def tree(session: AsyncSession, at: datetime | None = None) -> TeamTree:
    """The team tree, read at most once per session per `at` snapshot.

    Carries the teams themselves plus the id index, the display paths and the
    parent index that nearly every caller went on to rebuild by hand.

    The memo lives on the Session and is dropped the moment any Team row is
    inserted, changed or deleted in it (team_cache.py), so it cannot predate the
    caller's own write — provided that write has been flushed, which every
    mutation site here and in services.pages does before returning. Keyed by
    `at`, so a snapshot is never served for a live read.
    """
    hit = team_cache.cached(session, at)
    if hit is not None:
        return hit
    T = entity(Team, at)
    rows = await fetch(session, sa.select(T).order_by(T.name), at)
    built = build_tree([row[0] for row in rows])
    team_cache.store(session, at, built)
    return built


def build_tree(teams: Sequence[Team]) -> TeamTree:
    """A TeamTree over teams already in hand. tree() is the door for callers with
    a session; this one is for the pure tests and anyone holding a plain list."""
    return TeamTree(
        teams=tuple(teams),
        by_id={t.id: t for t in teams},
        paths=team_paths(teams),
        by_parent=children_map(teams),
    )


def children_map(teams: Sequence[Team]) -> dict[int | None, list[Team]]:
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
    (case-insensitive), as (team, path) sorted by path. Done in Python over the
    session's tree: paths are computed recursively, and the whole table is ~60
    rows. No actor: the team directory is visible to every signed-in user."""
    q = query.strip().lower()
    if not q:
        return []
    directory = await tree(session, at)
    paths = directory.paths
    hits = [
        (t, paths[t.id])
        for t in directory.teams
        if t.is_active
        and (
            q in t.name.lower()
            or q in (t.description or "").lower()
            or q in paths[t.id].lower()
        )
    ]
    hits.sort(key=lambda pair: pair[1].lower())
    return hits


async def meta_team_ids(
    session: AsyncSession, candidates: set[int] | None = None
) -> set[int]:
    """Team ids that are a live task force's meta team.

    A meta team is a borrowed roster copied from several real teams to staff one
    event (services.task_force); it confers rights over the EVENT, never over
    the people it borrowed. Every consumer that WIDENS scope over a team subtree
    — actors.load_actor, roster export, the Drive-sheet enumeration — must
    exclude these, or the collaboration hands out contact details it must not.
    One definition, so no consumer forgets. Pass `candidates` to restrict the
    check to a set of ids already in hand."""
    if candidates is not None and not candidates:
        return set()
    stmt = sa.select(Event.task_force_team_id).where(
        Event.task_force_team_id.is_not(None)
    )
    if candidates is not None:
        stmt = stmt.where(Event.task_force_team_id.in_(candidates))
    return set(await session.scalars(stmt))


def team_paths(teams: Sequence[Team]) -> dict[int, str]:
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


def _check_workload_weight(workload_weight: Decimal | None) -> Err[Invalid] | None:
    """Shared by create and update: they disagreed once, and a negative weight
    silently corrupts every workload band downstream."""
    if workload_weight is not None and workload_weight < 0:
        return invalid("workload weight must not be negative")
    return None


async def create(
    session: AsyncSession,
    actor: Actor | None,
    name: str,
    parent_team_id: int | None = None,
    description: str | None = None,
    workload_weight: Decimal | None = None,
) -> Result[Team, DomainError]:
    """Create a ministry. Admin-only — a team is the unit permissions hang off,
    so minting one is not a roster operation.

    `actor=None` is the trusted internal caller (the seed script, and
    services.task_force, which creates the meta team on behalf of whoever was
    already allowed to manage the event)."""
    if denied := require(actor is None or actor.is_admin, "only admins create teams"):
        return denied
    if bad := _check_workload_weight(workload_weight):
        return bad
    team = Team(
        name=name.strip(),
        parent_team_id=parent_team_id,
        description=description,
        workload_weight=Decimal(0) if workload_weight is None else workload_weight,
    )
    session.add(team)
    await session.flush()
    return Ok(team)


async def update(
    session: AsyncSession,
    actor: Actor | None,
    team_id: int,
    *,
    name: str | None = None,
    parent_team_id: int | None | object = _UNSET,
    description: str | None | object = _UNSET,
    is_active: bool | None = None,
    workload_weight: Decimal | None | object = _UNSET,
) -> Result[Team, DomainError]:
    if denied := require(actor is None or actor.is_admin, "only admins edit teams"):
        return denied
    team = await get(session, team_id)
    if team is None:
        return not_found("team", team_id)
    if name is not None:
        team.name = name.strip()
    if parent_team_id is not _UNSET:
        if cycle := await _check_no_cycle(session, team_id, parent_team_id):  # type: ignore[arg-type]
            return cycle
        team.parent_team_id = parent_team_id  # type: ignore[assignment]
    if description is not _UNSET:
        team.description = description  # type: ignore[assignment]
    if is_active is not None:
        team.is_active = is_active
    if workload_weight is not _UNSET:
        if bad := _check_workload_weight(workload_weight):  # type: ignore[arg-type]
            return bad
        # None from a caller means "no weight", which is 0 — the column has no
        # third state any more (models.Team.workload_weight)
        team.workload_weight = (
            Decimal(0) if workload_weight is None else workload_weight
        )  # type: ignore[assignment]
    await session.flush()
    return Ok(team)


async def _check_no_cycle(
    session: AsyncSession, team_id: int, new_parent_id: int | None
) -> Err[Invalid] | None:
    if new_parent_id is None:
        return None
    # The session's memo is safe to read here even mid-update: update() assigns
    # parent_team_id only AFTER this check, and any earlier update() in the same
    # transaction flushed, which drops the memo. So the parent edges below are
    # never behind the caller's own writes.
    subtree = (await tree(session)).descendants(team_id)
    if new_parent_id == team_id or new_parent_id in subtree:
        return invalid("a team cannot be its own ancestor")
    return None


async def _sheet_row(session: AsyncSession, team_id: int) -> TeamSheet:
    """The team's team_sheet row, created if the sync has not made one yet.
    Same upsert shape as roster_sheets.record_status."""
    sheet = await session.get(TeamSheet, team_id)
    if sheet is None:
        sheet = TeamSheet(team_id=team_id)
        session.add(sheet)
    return sheet


async def roster_sheet(
    session: AsyncSession, actor: Actor | None, team_id: int
) -> Result[TeamSheet | None, DomainError]:
    """The team's Drive roster sheet record, or None if the sync has not made
    one yet. Management rights, matching what the team page already shows."""
    if denied := require(
        actor is None or actor.can_manage_team(team_id),
        "see this team's roster spreadsheet",
    ):
        return denied
    return Ok(await session.get(TeamSheet, team_id))


async def set_roster_sheet(
    session: AsyncSession, actor: Actor | None, team_id: int, url: str
) -> Result[TeamSheet, DomainError]:
    """Point the team at the roster spreadsheet behind `url`.

    Leaders and seconds, not admins only. The old admin-only rule existed
    because a pasted link could not be checked until the next nightly sync —
    the app had no way to reach Drive at all — so handing it to leaders meant
    handing them a request nobody could validate. A link-shared sheet is
    readable the moment it is pasted, so the person who runs the ministry can
    do this themselves and see immediately whether it worked.

    Still narrower than pages.set_home_doc_url, which core members may use:
    that one publishes a page anybody may read, while a roster sheet carries
    every member's address, phone and notes, and syncing one hands it a bulk
    write over the roster.

    Only the id is stored. The URL is derived from it (sheets.common.sheet_url)
    so a link keeps working when Google rewrites its own URL shapes, and the
    caller is expected to sync afterwards — this function touches no network.
    """
    if denied := require(
        actor is None or actor.can_manage_team(team_id),
        "change this team's roster spreadsheet",
    ):
        return denied
    team = await get(session, team_id)
    if team is None:
        return not_found("team", team_id)
    parsed = extract_spreadsheet_id(url)
    if isinstance(parsed, Err):
        return parsed
    file_id = parsed.value
    # a sheet already spoken for is taken: two teams on one sheet would each
    # overwrite the other's roster every night
    clash = await session.scalar(
        sa.select(TeamSheet).where(
            TeamSheet.file_id == file_id,
            TeamSheet.team_id != team_id,
        )
    )
    if clash is not None:
        other = await get(session, clash.team_id)
        return invalid(
            f"that spreadsheet already belongs to "
            f"{other.name if other else 'another team'} — a sheet syncs with "
            "exactly one team"
        )
    sheet = await _sheet_row(session, team_id)
    if sheet.file_id == file_id:
        return invalid(f"{team.name} already syncs with that spreadsheet")
    sheet.file_id = file_id
    sheet.file_name = None  # the sync fills this in from the sheet itself
    sheet.last_synced_at = None
    sheet.last_status = None
    sheet.last_error = None
    await session.flush()
    return Ok(sheet)


async def leader_emails(session: AsyncSession, team_id: int) -> list[str]:
    """Emails of the team's leader(s) and second(s): who the nightly Drive sync
    grants edit access to the team's roster sheet, and who an event's
    notifications go to. Lowercased and blank-free — imports leave '' where a
    roster had no address, and neither a mailer nor a Drive share can do
    anything with it."""
    rows = await session.scalars(
        sa.select(Volunteer.email)
        .join(Membership, Membership.volunteer_id == Volunteer.id)
        .where(
            Membership.team_id == team_id,
            Membership.role.in_([TeamRole.leader, TeamRole.second]),
            Volunteer.email.is_not(None),
            Volunteer.email != "",
        )
    )
    return sorted({email.strip().lower() for email in rows if email.strip()})


async def delete(
    session: AsyncSession, actor: Actor | None, team_id: int
) -> Result[None, DomainError]:
    if denied := require(actor is None or actor.is_admin, "only admins delete teams"):
        return denied
    team = await get(session, team_id)
    if team is None:
        return not_found("team", team_id)
    # a live task force is torn down by its event, never deleted directly —
    # neither the meta team (event.team_id CASCADEs, so a direct delete takes
    # the event and its attendance record with it) NOR the owner team (the meta
    # team is its child under an ON DELETE RESTRICT parent FK, so deleting the
    # owner would otherwise die as a raw IntegrityError instead of this sentence)
    live = await session.scalar(
        sa.select(Event.id).where(
            sa.or_(
                Event.task_force_team_id == team_id,
                Event.owner_team_id == team_id,
            )
        )
    )
    if live is not None:
        return invalid(
            f"this team is part of the task force staffing event {live} — it is "
            "removed automatically after the event ends (manage it from the "
            "event page)"
        )
    await session.delete(team)
    await session.flush()
    return Ok(None)


async def roster(
    session: AsyncSession,
    actor: Actor | None,
    team_id: int,
    at: datetime | None = None,
) -> Result[list[tuple[Membership, Volunteer]], DomainError]:
    """Memberships with their volunteers, leaders first.

    Roster-NAMES rights: this returns whole Volunteer rows, and it is the
    caller's job to redact the contact fields a viewer may not read
    (api.volunteers.redacted, and the tiers the team page renders). The check
    here is the one that decides whether the roster exists for you at all."""
    if denied := require(
        actor is None or actor.can_view_roster_names(team_id),
        "view this team's roster",
    ):
        return denied
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
    return Ok([(m, v) for m, v in await fetch(session, stmt, at)])
