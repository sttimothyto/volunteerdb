"""Spreadsheet import: all-or-nothing, non-destructive, dry-run capable.

- Volunteers are matched by email (plus name to break family-shared-email
  ties), falling back to exact full name. Unmatched rows create volunteers.
- Memberships are upserted; rows never delete existing memberships.
- Any error rolls the whole import back; the report lists every issue.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO

import sqlalchemy as sa
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import db_session
from ..models import Team, Volunteer
from ..services import memberships as membership_service
from ..services import teams as team_service
from .common import MEMBERSHIP_SHEET, VOLUNTEER_HEADERS, VOLUNTEER_SHEET, parse_role


@dataclass
class Issue:
    sheet: str
    row: int
    message: str


@dataclass
class ImportReport:
    volunteers_created: int = 0
    volunteers_updated: int = 0
    memberships_created: int = 0
    memberships_updated: int = 0
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


class _Abort(Exception):
    def __init__(self, report: ImportReport):
        self.report = report


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("'="):
        text = text[1:]  # undo the exporter's formula-injection escape
    return text or None


def _parse_date(value, report: ImportReport, sheet: str, row: int) -> date | None:
    if value is None or _clean(value) is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        report.warnings.append(Issue(sheet, row, f"unreadable date {value!r}, ignored"))
        return None


async def run_import(content: bytes, *, dry_run: bool, user_id: int | None) -> ImportReport:
    """Parse and apply a workbook. On dry_run or any error, everything rolls back."""
    report = ImportReport()
    try:
        workbook = load_workbook(BytesIO(content), data_only=True)
    except Exception as exc:
        report.errors.append(Issue("-", 0, f"cannot read workbook: {exc}"))
        return report

    try:
        async with db_session(user_id) as session:
            await _apply(session, workbook, report)
            if dry_run or report.has_errors:
                raise _Abort(report)
            report.applied = True
    except _Abort:
        pass
    return report


async def _apply(session: AsyncSession, workbook, report: ImportReport) -> None:
    volunteers = list((await session.execute(sa.select(Volunteer))).scalars())
    by_email: dict[str, list[Volunteer]] = {}
    by_name: dict[str, list[Volunteer]] = {}

    def index(v: Volunteer) -> None:
        if v.email:
            by_email.setdefault(v.email.lower(), []).append(v)
        by_name.setdefault(v.full_name.lower(), []).append(v)

    for v in volunteers:
        index(v)

    def match(email: str | None, name: str | None) -> Volunteer | None | str:
        """A volunteer, None (no match), or an error message string."""
        candidates: list[Volunteer] = []
        if email:
            candidates = by_email.get(email.lower(), [])
            if len(candidates) > 1 and name:
                narrowed = [c for c in candidates if c.full_name.lower() == name.lower()]
                if narrowed:
                    candidates = narrowed
        elif name:
            candidates = by_name.get(name.lower(), [])
        if len(candidates) > 1:
            return f"ambiguous: {len(candidates)} volunteers match {email or name!r}"
        return candidates[0] if candidates else None

    # --- Volunteers sheet ---
    if VOLUNTEER_SHEET in workbook.sheetnames:
        header = next(
            workbook[VOLUNTEER_SHEET].iter_rows(min_row=1, max_row=1, values_only=True), ()
        )
        if sum(1 for cell in header if cell is not None) > len(VOLUNTEER_HEADERS):
            report.warnings.append(
                Issue(
                    VOLUNTEER_SHEET,
                    1,
                    "extra columns (custom fields) are ignored — custom values "
                    "are not imported yet",
                )
            )
        for row_num, row in enumerate(
            workbook[VOLUNTEER_SHEET].iter_rows(min_row=2, values_only=True), start=2
        ):
            row = tuple(row) + (None,) * (6 - len(row))
            first, last, email, phone, notes, active = (_clean(c) for c in row[:6])
            if not first and not last:
                continue
            if not first or not last:
                report.errors.append(
                    Issue(VOLUNTEER_SHEET, row_num, "first and last name are both required")
                )
                continue
            email = email.lower() if email else None
            found = match(email, f"{first} {last}")
            if isinstance(found, str):
                report.errors.append(Issue(VOLUNTEER_SHEET, row_num, found))
                continue
            is_active = active is None or active.lower() in ("yes", "y", "true", "1", "x")
            if found is None:
                v = Volunteer(
                    first_name=first,
                    last_name=last,
                    email=email,
                    phone=phone,
                    notes=notes,
                    is_active=is_active,
                )
                session.add(v)
                await session.flush()
                index(v)
                report.volunteers_created += 1
            else:
                changed = False
                for attr, value in (
                    ("first_name", first),
                    ("last_name", last),
                    ("email", email),
                    ("phone", phone),
                    ("notes", notes),
                    ("is_active", is_active),
                ):
                    if value is not None and getattr(found, attr) != value:
                        setattr(found, attr, value)
                        changed = True
                if changed:
                    report.volunteers_updated += 1

    # --- Memberships sheet ---
    if MEMBERSHIP_SHEET in workbook.sheetnames:
        all_teams = await team_service.list_all(session)
        paths = team_service.team_paths(all_teams)
        team_by_path = {p.lower(): tid for tid, p in paths.items()}
        team_by_name: dict[str, list[int]] = {}
        for t in all_teams:
            team_by_name.setdefault(t.name.lower(), []).append(t.id)

        for row_num, row in enumerate(
            workbook[MEMBERSHIP_SHEET].iter_rows(min_row=2, values_only=True), start=2
        ):
            row = tuple(row) + (None,) * (6 - len(row))
            email, name, team_path, role_raw, joined_raw, notes = row[:6]
            email, name, team_path = _clean(email), _clean(name), _clean(team_path)
            if not any((email, name, team_path, _clean(role_raw))):
                continue
            if not team_path:
                report.errors.append(Issue(MEMBERSHIP_SHEET, row_num, "team path is required"))
                continue

            team_id = team_by_path.get(team_path.lower())
            if team_id is None:
                ids = team_by_name.get(team_path.lower(), [])
                if len(ids) == 1:
                    team_id = ids[0]
                elif len(ids) > 1:
                    report.errors.append(
                        Issue(
                            MEMBERSHIP_SHEET,
                            row_num,
                            f"team name {team_path!r} is ambiguous, use its full path",
                        )
                    )
                    continue
            if team_id is None:
                report.errors.append(
                    Issue(MEMBERSHIP_SHEET, row_num, f"unknown team {team_path!r}")
                )
                continue

            role = parse_role(role_raw) if role_raw is not None else None
            if role is None:
                report.errors.append(
                    Issue(MEMBERSHIP_SHEET, row_num, f"unknown role {role_raw!r}")
                )
                continue

            found = match(email, name)
            if isinstance(found, str):
                report.errors.append(Issue(MEMBERSHIP_SHEET, row_num, found))
                continue
            if found is None:
                report.errors.append(
                    Issue(
                        MEMBERSHIP_SHEET,
                        row_num,
                        f"unknown volunteer {email or name!r} (add them to the Volunteers sheet)",
                    )
                )
                continue

            joined_on = _parse_date(joined_raw, report, MEMBERSHIP_SHEET, row_num)
            existing = await membership_service.find(session, found.id, team_id)
            before = (existing.role, existing.joined_on, existing.notes) if existing else None
            membership = await membership_service.assign(
                session, found.id, team_id, role, joined_on=joined_on, notes=_clean(notes)
            )
            if existing is None:
                report.memberships_created += 1
            elif before != (membership.role, membership.joined_on, membership.notes):
                report.memberships_updated += 1
