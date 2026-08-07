from fastapi import APIRouter, HTTPException, Response, UploadFile
from pydantic import BaseModel

from ..permissions import require
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


@router.get("/export/template.csv")
async def template_csv(ctx: CtxDep) -> Response:
    return _csv(exporter.template_csv(), "volunteerdb-template.csv")


@router.get("/export/parish.csv")
async def export_parish(ctx: CtxDep, as_of: AsOf) -> Response:
    require(ctx.actor.is_admin, "export the whole parish")
    content = await exporter.export_csv(ctx.session, at=as_of)
    return _csv(content, "volunteerdb-parish.csv")


@router.get("/export/team/{team_id}.csv")
async def export_team(ctx: CtxDep, team_id: int, as_of: AsOf) -> Response:
    require(ctx.actor.can_view_full_roster(team_id), "export this team")
    content = await exporter.export_csv(ctx.session, team_id=team_id, at=as_of)
    return _csv(content, f"volunteerdb-team-{team_id}.csv")


@router.get("/export/my-teams.csv")
async def export_my_teams(ctx: CtxDep, as_of: AsOf) -> Response:
    """Union of the teams the caller leads or seconds, incl. sub-teams."""
    require(bool(ctx.actor.managed_team_ids), "export the teams you lead")
    content = await exporter.export_csv(
        ctx.session, team_ids=ctx.actor.managed_team_ids, at=as_of
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
    ctx: CtxDep, file: UploadFile, dry_run: bool = False
) -> ImportReportOut:
    """Upload a filled-in roster .csv. All-or-nothing; use dry_run=true to
    preview. Non-admin leaders/seconds are scoped to the teams they manage
    (out-of-scope rows come back as errors)."""
    require(ctx.actor.can_import_export, "import spreadsheets")
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "file larger than 10 MB")
    report = await importer.run_import(
        content, dry_run=dry_run, user_id=ctx.actor.user.id
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
