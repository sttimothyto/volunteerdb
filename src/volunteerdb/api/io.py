from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel

from ..log import audit_log
from ..permissions import require
from ..services import mail as mail_service
from ..sheets import exporter, importer
from .deps import AsOf, CtxDep

router = APIRouter(tags=["import/export"])

MAX_IMPORT_BYTES = 10_000_000  # same cap the UI upload enforces


def _csv(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/parish.csv")
async def export_parish(ctx: CtxDep, as_of: AsOf) -> Response:
    require(ctx.actor.is_admin, "export the whole parish")
    audit_log("export.parish", as_of=as_of.isoformat() if as_of else None)
    content = await exporter.export_csv(ctx.session, at=as_of)
    return _csv(content, "volunteerdb-parish.csv")


@router.get("/export/team/{team_id}.csv")
async def export_team(ctx: CtxDep, team_id: int, as_of: AsOf) -> Response:
    """Roster export, sub-teams included.

    The notes column comes through only for someone who may read notes.
    Everywhere else `notes` needs can_edit_volunteer — `redacted()` in
    api/volunteers.py, the roster in api/teams.py, both GUI views — but the CSV
    handed the whole column to any core member who may see the roster, which is
    the one place leadership's private remarks escaped their audience."""
    require(ctx.actor.can_view_full_roster(team_id), "export this team")
    notes = ctx.actor.is_admin or ctx.actor.can_manage_team(team_id)
    audit_log(
        "export.team_roster",
        team_id=team_id,
        as_of=as_of.isoformat() if as_of else None,
        notes_included=notes,
    )
    content = await exporter.export_csv(
        ctx.session,
        team_id=team_id,
        at=as_of,
        include_notes=notes,
    )
    return _csv(content, f"volunteerdb-team-{team_id}.csv")


@router.get("/export/my-teams.csv")
async def export_my_teams(ctx: CtxDep, as_of: AsOf) -> Response:
    """Union of the teams the caller leads or seconds, incl. sub-teams.

    people_team_ids, not managed_team_ids: this export carries contact details,
    so a task force the caller happens to run must not smuggle out the rosters
    it borrowed (permissions.Actor)."""
    require(bool(ctx.actor.people_team_ids), "export the teams you lead")
    audit_log(
        "export.my_teams",
        team_ids=sorted(ctx.actor.people_team_ids),
        as_of=as_of.isoformat() if as_of else None,
    )
    content = await exporter.export_csv(
        ctx.session, team_ids=ctx.actor.people_team_ids, at=as_of
    )
    return _csv(content, "volunteerdb-my-teams.csv")


class IssueOut(BaseModel):
    sheet: str
    row: int
    message: str


class ImportReportOut(BaseModel):
    applied: bool
    volunteers_created: int
    volunteers_updated: int
    volunteers_reactivated: int
    memberships_created: int
    memberships_updated: int
    errors: list[IssueOut]
    warnings: list[IssueOut]


@router.post("/import")
async def import_roster(
    ctx: CtxDep,
    file: UploadFile,
    request: Request,
    background: BackgroundTasks,
    dry_run: bool = False,
) -> ImportReportOut:
    """Upload a filled-in roster .csv. All-or-nothing; use dry_run=true to
    preview. Non-admin leaders/seconds are scoped to the teams they manage
    (out-of-scope rows come back as errors).

    A row that *redirects* an address mails the mailbox it moved away from
    (services.mail.address_edited_email) — the one message this route sends,
    for the same reason the invite route sends one."""
    require(ctx.actor.can_import_export, "import spreadsheets")
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "file larger than 10 MB")
    report = await importer.run_import(
        content, dry_run=dry_run, user_id=ctx.actor.user.id
    )
    if report.applied and report.addresses_replaced:
        background.add_task(
            mail_service.notify_replaced_addresses,
            report.addresses_replaced,
            f"{str(request.base_url).rstrip('/')}/login",
        )
    return ImportReportOut(
        applied=report.applied,
        volunteers_created=report.volunteers_created,
        volunteers_updated=report.volunteers_updated,
        volunteers_reactivated=report.volunteers_reactivated,
        memberships_created=report.memberships_created,
        memberships_updated=report.memberships_updated,
        errors=[IssueOut(**vars(i)) for i in report.errors],
        warnings=[IssueOut(**vars(i)) for i in report.warnings],
    )
