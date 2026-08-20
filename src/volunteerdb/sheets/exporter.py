import csv
from dataclasses import dataclass
from datetime import datetime
from io import StringIO

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity, fetch
from ..log import audit_log
from ..models import ROLE_LABELS, CustomFieldDef, FieldType, Membership, Volunteer
from ..permissions import Actor, require
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
    include_notes: bool = True,
) -> RosterData:
    """One row per membership, ordered by (team path, volunteer name).

    include_notes=False blanks the "Volunteer notes" column while keeping it in
    place, for an exporter allowed to read the roster but not the notes on it
    (a core member: everywhere else `notes` needs can_edit_volunteer). The
    column stays because the file has to round-trip — and it does, because a
    blank cell parses to None and the importer leaves a None field alone
    (sheets/common.clean_cell, importer.apply_rows).

    Scope: whole parish (team_ids=None, which also appends volunteers with no
    membership as rows with blank team columns), or the union of the given
    teams' subtrees. An empty set is an error — it must never widen to a
    parish export. subtree=False is the Drive-sheet mode: direct memberships
    of exactly the given teams, no unassigned rows, no custom-field columns —
    the leader-editable sheet stays minimal.
    """
    if team_ids is not None and not team_ids:
        raise ValueError("team_ids must be None (parish) or a non-empty set")
    tree = await team_service.tree(session, at=at)
    paths = tree.paths
    if team_ids is None:
        include_ids = {t.id for t in tree.teams}
    elif subtree:
        include_ids = set()
        for team_id in team_ids:
            include_ids |= tree.descendants(team_id)
    else:
        include_ids = team_ids
    if team_ids is None or subtree:
        # A task-force meta team is a borrowed roster the exporter's rights do
        # not cascade into (permissions.load_actor makes the same cut). Dropping
        # it here stops a scoped subtree export — authorized on the owner team
        # alone — from leaking the collaborating teams' contact details through
        # the meta team that sits under the owner, and stops a parish export from
        # duplicating people under the transient task-force team. The explicit
        # subtree=False mode (the Drive sheet) names its teams exactly and is
        # trusted to; drive_sync never hands it a meta team.
        include_ids -= await team_service.meta_team_ids(session, include_ids)

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
            v.id,
            v.first_name,
            v.last_name,
            v.email,
            v.phone,
            v.notes if include_notes else None,
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
                *custom_cells(v),
            )
        ]
        for m, v in by_path
    ]

    if team_ids is None:
        # Active only: without an Active column an archived volunteer's row is
        # indistinguishable from a live one, and imports have no removal path,
        # so leaving them out cannot lose anything.
        assigned = {v.id for _, v in membership_pairs}
        unassigned = [
            row[0]
            for row in await fetch(
                session,
                sa.select(V).where(V.is_active).order_by(V.last_name, V.first_name),
                at,
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
    actor: Actor | None,
    *,
    team_id: int | None = None,
    team_ids: set[int] | None = None,
    at: datetime | None = None,
    subtree: bool = True,
) -> bytes:
    """Whole parish, one team's subtree (team_id), or a union of subtrees
    (team_ids); subtree=False restricts to direct memberships (Drive-sheet mode).

    Authorization is here rather than at the two front doors, because this is
    the widest read in the app and the rules are not obvious: a parish export
    is admin-only, a team export needs full-roster rights on that team, and a
    union needs them on every team in it. The notes column follows from the
    same actor — blank unless they may read notes — so no caller has to work
    that out, and the audit line is written once.

    `actor=None` is a trusted internal caller: the nightly Drive sync, which
    writes each team's own sheet.
    """
    if team_ids is None and team_id is not None:
        team_ids = {team_id}
    include_notes = True
    if actor is not None:
        if team_ids is None:
            require(actor.is_admin, "export the whole parish")
        else:
            for scope_id in team_ids:
                require(actor.can_view_full_roster(scope_id), "export this team")
            # Notes need edit rights everywhere else in the app, so a core
            # member — who may read the roster but not the notes on it — gets
            # the column blank. It stays in place because the file has to
            # round-trip: a blank cell parses to None and the importer leaves a
            # None field alone.
            include_notes = actor.is_admin or all(
                actor.can_manage_team(scope_id) for scope_id in team_ids
            )
        audit_log(
            "export.roster",
            scope="parish" if team_ids is None else sorted(team_ids),
            as_of=at.isoformat() if at else None,
            notes_included=include_notes,
        )
    data = await build_roster_rows(
        session,
        team_ids=team_ids,
        at=at,
        subtree=subtree,
        include_notes=include_notes,
    )
    return _csv_bytes(data.header, data.rows)
