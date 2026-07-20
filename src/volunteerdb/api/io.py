from fastapi import APIRouter, HTTPException, Response, UploadFile
from pydantic import BaseModel

from ..permissions import require
from ..sheets import exporter, importer
from .deps import AsOf, CtxDep

router = APIRouter(tags=["import/export"])

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_IMPORT_BYTES = 10_000_000  # same cap the UI upload enforces


def _xlsx(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type=XLSX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/template.xlsx")
async def template(ctx: CtxDep) -> Response:
    return _xlsx(exporter.template_workbook(), "volunteerdb-template.xlsx")


@router.get("/export/parish.xlsx")
async def export_parish(ctx: CtxDep, as_of: AsOf) -> Response:
    require(ctx.actor.is_admin, "export the whole parish")
    content = await exporter.export_workbook(ctx.session, at=as_of)
    return _xlsx(content, "volunteerdb-parish.xlsx")


@router.get("/export/team/{team_id}.xlsx")
async def export_team(ctx: CtxDep, team_id: int, as_of: AsOf) -> Response:
    require(ctx.actor.can_view_full_roster(team_id), "export this team")
    content = await exporter.export_workbook(ctx.session, team_id=team_id, at=as_of)
    return _xlsx(content, f"volunteerdb-team-{team_id}.xlsx")


class IssueOut(BaseModel):
    sheet: str
    row: int
    message: str


class ImportReportOut(BaseModel):
    applied: bool
    volunteers_created: int
    volunteers_updated: int
    memberships_created: int
    memberships_updated: int
    errors: list[IssueOut]
    warnings: list[IssueOut]


@router.post("/import")
async def import_workbook(ctx: CtxDep, file: UploadFile, dry_run: bool = False) -> ImportReportOut:
    """Upload a filled-in template. All-or-nothing; use dry_run=true to preview."""
    require(ctx.actor.is_admin, "import spreadsheets")
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "workbook larger than 10 MB")
    report = await importer.run_import(content, dry_run=dry_run, user_id=ctx.actor.user.id)
    return ImportReportOut(
        applied=report.applied,
        volunteers_created=report.volunteers_created,
        volunteers_updated=report.volunteers_updated,
        memberships_created=report.memberships_created,
        memberships_updated=report.memberships_updated,
        errors=[IssueOut(**vars(i)) for i in report.errors],
        warnings=[IssueOut(**vars(i)) for i in report.warnings],
    )
