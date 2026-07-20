import csv
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO, StringIO

import sqlalchemy as sa
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity, fetch
from ..models import ROLE_LABELS, CustomFieldDef, FieldType, Membership, TeamRole, Volunteer
from ..services import custom_fields as custom_field_service
from ..services import teams as team_service
from .common import MEMBERSHIP_HEADERS, MEMBERSHIP_SHEET, VOLUNTEER_HEADERS, VOLUNTEER_SHEET


def _workbook(custom_defs: list[CustomFieldDef] = ()) -> tuple[Workbook, Worksheet, Worksheet]:
    wb = Workbook()
    vs = wb.active
    vs.title = VOLUNTEER_SHEET
    vs.append([*VOLUNTEER_HEADERS, *(d.label for d in custom_defs)])
    ms = wb.create_sheet(MEMBERSHIP_SHEET)
    ms.append(MEMBERSHIP_HEADERS)
    bold = Font(bold=True)
    for sheet in (vs, ms):
        for cell in sheet[1]:
            cell.font = bold
    role_validation = DataValidation(
        type="list",
        formula1='"' + ",".join(ROLE_LABELS[r] for r in TeamRole) + '"',
        allow_blank=False,
        showDropDown=False,
    )
    ms.add_data_validation(role_validation)
    role_validation.add("D2:D1000")
    return wb, vs, ms


def _custom_cell(defn: CustomFieldDef, value):
    if value is None:
        return None
    if defn.field_type == FieldType.checkbox.value:
        return "yes" if value else "no"
    return value


def _safe(value):
    """Strings starting with '=' would become live formulas when opened in a
    spreadsheet program (xlsx and CSV alike); prefix a quote (stripped again
    by the importer on round-trip)."""
    if isinstance(value, str) and value.startswith("="):
        return "'" + value
    return value


def _to_bytes(wb: Workbook) -> bytes:
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    """UTF-8 with BOM so Excel opens it correctly without an import wizard."""
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


CSV_SHEETS = ("volunteers", "memberships")


def template_workbook() -> bytes:
    """Empty two-sheet template with headers and a role dropdown."""
    wb, _, _ = _workbook()
    return _to_bytes(wb)


def template_csv(sheet: str) -> bytes:
    """Header-only CSV for one sheet ('volunteers' or 'memberships').
    Unlike the xlsx template there is no role dropdown — CSV cannot carry one."""
    if sheet not in CSV_SHEETS:
        raise ValueError(f"unknown sheet {sheet!r}")
    header = VOLUNTEER_HEADERS if sheet == "volunteers" else MEMBERSHIP_HEADERS
    return _csv_bytes(header, [])


@dataclass
class ExportData:
    custom_defs: list[CustomFieldDef]
    volunteer_header: list[str]  # VOLUNTEER_HEADERS + custom-field labels
    volunteer_rows: list[list]  # cells already _safe-escaped
    membership_rows: list[list]


async def build_export_data(
    session: AsyncSession,
    *,
    team_ids: set[int] | None = None,
    at: datetime | None = None,
) -> ExportData:
    """Rows for both sheets: whole parish, or the union of the given teams'
    subtrees. An empty set is an error — it must never widen to a parish export."""
    if team_ids is not None and not team_ids:
        raise ValueError("team_ids must be None (parish) or a non-empty set")
    all_teams = await team_service.list_all(session, at=at)
    paths = team_service.team_paths(all_teams)
    if team_ids is None:
        include_ids = {t.id for t in all_teams}
    else:
        include_ids = set()
        for team_id in team_ids:
            include_ids |= team_service.descendant_ids(all_teams, team_id)

    M, V = entity(Membership, at), entity(Volunteer, at)
    membership_pairs = await fetch(
        session,
        sa.select(M, V)
        .join(V, V.id == M.volunteer_id)
        .where(M.team_id.in_(include_ids))
        .order_by(M.team_id),
        at,
    )

    if team_ids is None:
        volunteers = [
            row[0]
            for row in await fetch(
                session, sa.select(V).order_by(V.last_name, V.first_name), at
            )
        ]
    else:
        seen: set[int] = set()
        volunteers = []
        for _, v in membership_pairs:
            if v.id not in seen:
                seen.add(v.id)
                volunteers.append(v)
        volunteers.sort(key=lambda v: (v.last_name.lower(), v.first_name.lower()))

    custom_defs = await custom_field_service.list_defs(session)
    volunteer_rows = [
        [
            _safe(c)
            for c in (
                v.first_name,
                v.last_name,
                v.email,
                v.phone,
                v.notes,
                "yes" if v.is_active else "no",
                *(_custom_cell(d, (v.custom or {}).get(d.key)) for d in custom_defs),
            )
        ]
        for v in volunteers
    ]
    by_path = sorted(
        membership_pairs, key=lambda row: (paths[row[0].team_id].lower(), row[1].last_name.lower())
    )
    membership_rows = [
        [
            _safe(c)
            for c in (
                v.email,
                f"{v.first_name} {v.last_name}",
                paths[m.team_id],
                ROLE_LABELS[m.role],
                m.joined_on.isoformat() if m.joined_on else None,
                m.notes,
            )
        ]
        for m, v in by_path
    ]
    return ExportData(
        custom_defs=custom_defs,
        volunteer_header=[*VOLUNTEER_HEADERS, *(d.label for d in custom_defs)],
        volunteer_rows=volunteer_rows,
        membership_rows=membership_rows,
    )


def _resolve_scope(team_id: int | None, team_ids: set[int] | None) -> set[int] | None:
    if team_ids is not None:
        return team_ids
    return {team_id} if team_id is not None else None


async def export_workbook(
    session: AsyncSession,
    team_id: int | None = None,
    at: datetime | None = None,
    *,
    team_ids: set[int] | None = None,
) -> bytes:
    """Whole parish, one team's subtree (team_id), or a union of subtrees (team_ids)."""
    data = await build_export_data(session, team_ids=_resolve_scope(team_id, team_ids), at=at)
    wb, vs, ms = _workbook(data.custom_defs)
    for row in data.volunteer_rows:
        vs.append(row)
    for row in data.membership_rows:
        ms.append(row)
    return _to_bytes(wb)


async def export_csv(
    session: AsyncSession,
    sheet: str,
    *,
    team_id: int | None = None,
    team_ids: set[int] | None = None,
    at: datetime | None = None,
) -> bytes:
    """One sheet ('volunteers' or 'memberships') as CSV, same scoping as export_workbook."""
    if sheet not in CSV_SHEETS:
        raise ValueError(f"unknown sheet {sheet!r}")
    data = await build_export_data(session, team_ids=_resolve_scope(team_id, team_ids), at=at)
    if sheet == "volunteers":
        return _csv_bytes(data.volunteer_header, data.volunteer_rows)
    return _csv_bytes(MEMBERSHIP_HEADERS, data.membership_rows)
