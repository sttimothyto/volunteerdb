import csv
from dataclasses import dataclass
from datetime import datetime
from io import StringIO

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity, fetch
from ..models import ROLE_LABELS, CustomFieldDef, FieldType, Membership, Volunteer
from ..services import custom_fields as custom_field_service
from ..services import teams as team_service
from .common import ROSTER_HEADERS, safe_cell


def _custom_cell(defn: CustomFieldDef, value):
    if value is None:
        return None
    if defn.field_type == FieldType.checkbox.value:
        return "yes" if value else "no"
    return value


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    """UTF-8 with BOM so Excel opens it correctly without an import wizard."""
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def template_csv() -> bytes:
    """Header-only roster CSV."""
    return _csv_bytes(ROSTER_HEADERS, [])


@dataclass
class RosterData:
    header: list[str]  # ROSTER_HEADERS + custom-field labels (export-only)
    rows: list[list]  # cells already safe_cell-escaped


async def build_roster_rows(
    session: AsyncSession,
    *,
    team_ids: set[int] | None = None,
    at: datetime | None = None,
    subtree: bool = True,
) -> RosterData:
    """One row per membership, ordered by (team path, volunteer name).

    Scope: whole parish (team_ids=None, which also appends volunteers with no
    membership as rows with blank team columns), or the union of the given
    teams' subtrees. An empty set is an error — it must never widen to a
    parish export. subtree=False is the Drive-sheet mode: direct memberships
    of exactly the given teams, no unassigned rows, no custom-field columns —
    the leader-editable sheet stays minimal.
    """
    if team_ids is not None and not team_ids:
        raise ValueError("team_ids must be None (parish) or a non-empty set")
    all_teams = await team_service.list_all(session, at=at)
    paths = team_service.team_paths(all_teams)
    if team_ids is None:
        include_ids = {t.id for t in all_teams}
    elif subtree:
        include_ids = set()
        for team_id in team_ids:
            include_ids |= team_service.descendant_ids(all_teams, team_id)
    else:
        include_ids = team_ids

    M, V = entity(Membership, at), entity(Volunteer, at)
    membership_pairs = await fetch(
        session,
        sa.select(M, V)
        .join(V, V.id == M.volunteer_id)
        .where(M.team_id.in_(include_ids))
        .order_by(M.team_id),
        at,
    )

    custom_defs = await custom_field_service.list_defs(session) if subtree else []

    def volunteer_cells(v: Volunteer) -> tuple:
        return (
            v.first_name,
            v.last_name,
            v.email,
            v.phone,
            v.notes,
            "yes" if v.is_active else "no",
        )

    def custom_cells(v: Volunteer) -> tuple:
        return tuple(_custom_cell(d, (v.custom or {}).get(d.key)) for d in custom_defs)

    by_path = sorted(
        membership_pairs,
        key=lambda row: (
            paths[row[0].team_id].lower(),
            row[1].last_name.lower(),
            row[1].first_name.lower(),
        ),
    )
    rows = [
        [
            safe_cell(c)
            for c in (
                *volunteer_cells(v),
                paths[m.team_id],
                ROLE_LABELS[m.role],
                m.joined_on.isoformat() if m.joined_on else None,
                m.notes,
                *custom_cells(v),
            )
        ]
        for m, v in by_path
    ]

    if team_ids is None:
        assigned = {v.id for _, v in membership_pairs}
        unassigned = [
            row[0]
            for row in await fetch(
                session, sa.select(V).order_by(V.last_name, V.first_name), at
            )
            if row[0].id not in assigned
        ]
        rows += [
            [
                safe_cell(c)
                for c in (
                    *volunteer_cells(v),
                    None,  # Team
                    None,  # Role
                    None,  # Joined on
                    None,  # Membership notes
                    *custom_cells(v),
                )
            ]
            for v in unassigned
        ]

    return RosterData(
        header=[*ROSTER_HEADERS, *(d.label for d in custom_defs)],
        rows=rows,
    )


async def export_csv(
    session: AsyncSession,
    *,
    team_id: int | None = None,
    team_ids: set[int] | None = None,
    at: datetime | None = None,
    subtree: bool = True,
) -> bytes:
    """Whole parish, one team's subtree (team_id), or a union of subtrees
    (team_ids); subtree=False restricts to direct memberships (Drive-sheet mode).
    """
    if team_ids is None and team_id is not None:
        team_ids = {team_id}
    data = await build_roster_rows(session, team_ids=team_ids, at=at, subtree=subtree)
    return _csv_bytes(data.header, data.rows)
