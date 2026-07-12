from datetime import datetime
from io import BytesIO

import sqlalchemy as sa
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity, fetch
from ..models import ROLE_LABELS, Membership, Team, TeamRole, Volunteer
from ..services import teams as team_service
from .common import MEMBERSHIP_HEADERS, MEMBERSHIP_SHEET, VOLUNTEER_HEADERS, VOLUNTEER_SHEET


def _workbook() -> tuple[Workbook, Worksheet, Worksheet]:
    wb = Workbook()
    vs = wb.active
    vs.title = VOLUNTEER_SHEET
    vs.append(VOLUNTEER_HEADERS)
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


def _to_bytes(wb: Workbook) -> bytes:
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def template_workbook() -> bytes:
    """Empty two-sheet template with headers and a role dropdown."""
    wb, _, _ = _workbook()
    return _to_bytes(wb)


async def export_workbook(
    session: AsyncSession,
    team_id: int | None = None,
    at: datetime | None = None,
) -> bytes:
    """Whole parish, or one team's subtree when team_id is given."""
    all_teams = await team_service.list_all(session, at=at)
    paths = team_service.team_paths(all_teams)
    include_ids = (
        team_service.descendant_ids(all_teams, team_id)
        if team_id is not None
        else {t.id for t in all_teams}
    )

    M, V = entity(Membership, at), entity(Volunteer, at)
    membership_rows = await fetch(
        session,
        sa.select(M, V)
        .join(V, V.id == M.volunteer_id)
        .where(M.team_id.in_(include_ids))
        .order_by(M.team_id),
        at,
    )

    if team_id is None:
        volunteers = [
            row[0]
            for row in await fetch(
                session, sa.select(V).order_by(V.last_name, V.first_name), at
            )
        ]
    else:
        seen: set[int] = set()
        volunteers = []
        for _, v in membership_rows:
            if v.id not in seen:
                seen.add(v.id)
                volunteers.append(v)
        volunteers.sort(key=lambda v: (v.last_name.lower(), v.first_name.lower()))

    wb, vs, ms = _workbook()
    for v in volunteers:
        vs.append([v.first_name, v.last_name, v.email, v.phone, v.notes, "yes" if v.is_active else "no"])
    by_path = sorted(
        membership_rows, key=lambda row: (paths[row[0].team_id].lower(), row[1].last_name.lower())
    )
    for m, v in by_path:
        ms.append(
            [
                v.email,
                f"{v.first_name} {v.last_name}",
                paths[m.team_id],
                ROLE_LABELS[m.role],
                m.joined_on.isoformat() if m.joined_on else None,
                m.notes,
            ]
        )
    return _to_bytes(wb)
