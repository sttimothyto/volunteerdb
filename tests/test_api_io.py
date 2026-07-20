"""Spreadsheet export/import over HTTP: permissions, size cap, dry-run."""

from io import BytesIO

from openpyxl import load_workbook

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, volunteers
from volunteerdb.sheets import exporter
from volunteerdb.sheets.common import (
    MEMBERSHIP_HEADERS,
    MEMBERSHIP_SHEET,
    VOLUNTEER_HEADERS,
    VOLUNTEER_SHEET,
)

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _upload(content: bytes) -> dict:
    return {"file": ("import.xlsx", content, XLSX_TYPE)}


def _import_workbook_bytes(rows_volunteers: list, rows_memberships: list) -> bytes:
    wb = load_workbook(BytesIO(exporter.template_workbook()))
    for row in rows_volunteers:
        wb[VOLUNTEER_SHEET].append(row)
    for row in rows_memberships:
        wb[MEMBERSHIP_SHEET].append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def test_template_download(client, seeded, token_member):
    r = await client.get("/api/export/template.xlsx", headers=token_member)
    assert r.status_code == 200, "any signed-in user may fetch the template"
    assert r.headers["content-type"] == XLSX_TYPE
    assert 'filename="volunteerdb-template.xlsx"' in r.headers["content-disposition"]

    wb = load_workbook(BytesIO(r.content))
    assert set(wb.sheetnames) == {VOLUNTEER_SHEET, MEMBERSHIP_SHEET}
    assert [c.value for c in wb[VOLUNTEER_SHEET][1]] == VOLUNTEER_HEADERS
    assert [c.value for c in wb[MEMBERSHIP_SHEET][1]] == MEMBERSHIP_HEADERS


async def test_parish_export_admin_only(client, seeded, token_admin, token_member):
    r = await client.get("/api/export/parish.xlsx", headers=token_member)
    assert r.status_code == 403

    r = await client.get("/api/export/parish.xlsx", headers=token_admin)
    assert r.status_code == 200
    vs = load_workbook(BytesIO(r.content))[VOLUNTEER_SHEET]
    names = {(row[0], row[1]) for row in vs.iter_rows(min_row=2, values_only=True)}
    assert ("Maria", "Alvarez") in names


async def test_team_export_permission_matrix(client, seeded, token_admin, token_member):
    team_id = seeded["team_id"]

    r = await client.get(f"/api/export/team/{team_id}.xlsx", headers=token_member)
    assert r.status_code == 403, "a plain member cannot export contact details"

    async with db_session() as session:
        # promote the member's volunteer to core: full-roster rights incl. sub-teams
        await memberships.assign(session, seeded["volunteer_id"], team_id, TeamRole.core)
        music = await teams.create(session, "Music", parent_team_id=team_id)
        singer = await volunteers.create(session, "Sally", "Singer", "sally@example.org")
        await memberships.assign(session, singer.id, music.id, TeamRole.member)

    r = await client.get(f"/api/export/team/{team_id}.xlsx", headers=token_member)
    assert r.status_code == 200
    ms = load_workbook(BytesIO(r.content))[MEMBERSHIP_SHEET]
    paths = {row[2] for row in ms.iter_rows(min_row=2, values_only=True)}
    assert paths == {"Liturgy", "Liturgy / Music"}, "subtree included"

    r = await client.get(f"/api/export/team/{team_id}.xlsx", headers=token_admin)
    assert r.status_code == 200


async def test_import_dry_run_then_apply_over_http(client, seeded, token_admin, token_member):
    content = _import_workbook_bytes(
        [["Eve", "Green", "eve@example.org", None, None, "yes"]],
        [["eve@example.org", "Eve Green", "Liturgy", "leader", None, None]],
    )

    r = await client.post("/api/import", files=_upload(content), headers=token_member)
    assert r.status_code == 403

    r = await client.post(
        "/api/import", params={"dry_run": "true"}, files=_upload(content), headers=token_admin
    )
    assert r.status_code == 200
    report = r.json()
    assert report["applied"] is False
    assert report["volunteers_created"] == 1 and report["memberships_created"] == 1
    async with db_session() as session:
        assert await volunteers.search(session, "Eve") == [], "dry run wrote nothing"

    r = await client.post("/api/import", files=_upload(content), headers=token_admin)
    assert r.status_code == 200
    assert r.json()["applied"] is True
    async with db_session() as session:
        (eve,) = await volunteers.search(session, "Eve")
        assert eve.email == "eve@example.org"


async def test_import_oversize_rejected_413(client, seeded, token_admin):
    r = await client.post(
        "/api/import", files=_upload(b"x" * 10_000_001), headers=token_admin
    )
    assert r.status_code == 413


async def test_import_unreadable_workbook_reports_error(client, seeded, token_admin):
    r = await client.post(
        "/api/import", files=_upload(b"this is not a workbook"), headers=token_admin
    )
    assert r.status_code == 200
    report = r.json()
    assert report["applied"] is False
    assert report["errors"] and "cannot read workbook" in report["errors"][0]["message"]
