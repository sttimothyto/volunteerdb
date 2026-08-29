"""Roster CSV export/import over HTTP: permissions, size cap, dry-run."""

import csv
from io import StringIO

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, volunteers
from volunteerdb.sheets.common import ROSTER_HEADERS

from tests.fp_helpers import ok


def _upload(content: bytes) -> dict:
    return {"file": ("import.csv", content, "text/csv")}


def _csv_bytes(rows: list[list]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ROSTER_HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(StringIO(content.decode("utf-8-sig"))))


async def test_template_route_removed(client, seeded, token_member):
    """The CSV template endpoint gave way to the decorated Google Sheet on
    Drive (VDB_TEMPLATE_SHEET_URL); exporter.template_csv() lives on for the
    dev fallback on /import and for authoring the Drive template.

    Deliberately NOT restored when the API/GUI parity gaps were closed: a
    header-only CSV is the worse of the two answers now, and the column layout
    it would document is in the spreadsheet reference either way."""
    r = await client.get("/api/export/template.csv", headers=token_member)
    assert r.status_code == 404


async def test_parish_export_admin_only(client, seeded, token_admin, token_member):
    r = await client.get("/api/export/parish.csv", headers=token_member)
    assert r.status_code == 403

    r = await client.get("/api/export/parish.csv", headers=token_admin)
    assert r.status_code == 200
    assert 'filename="volunteerdb-parish.csv"' in r.headers["content-disposition"]
    names = {(row[1], row[2]) for row in _rows(r.content)[1:]}
    assert ("Maria", "Alvarez") in names


async def test_team_export_permission_matrix(client, seeded, token_admin, token_member):
    team_id = seeded["team_id"]

    r = await client.get(f"/api/export/team/{team_id}.csv", headers=token_member)
    assert r.status_code == 403, "a plain member cannot export contact details"

    async with db_session() as session:
        # promote the member's volunteer to core: full-roster rights incl. sub-teams
        ok(
            await memberships.assign(
                session, None, seeded["volunteer_id"], team_id, TeamRole.core
            )
        )
        music = ok(await teams.create(session, None, "Music", parent_team_id=team_id))
        singer = await volunteers.create(
            session, None, "Sally", "Singer", "sally@example.org"
        )
        ok(
            await memberships.assign(
                session, None, singer.id, music.id, TeamRole.member
            )
        )

    r = await client.get(f"/api/export/team/{team_id}.csv", headers=token_member)
    assert r.status_code == 200
    paths = {row[6] for row in _rows(r.content)[1:]}
    assert paths == {"Liturgy", "Liturgy / Music"}, "subtree included"

    r = await client.get(f"/api/export/team/{team_id}.csv", headers=token_admin)
    assert r.status_code == 200


async def test_my_teams_export(client, seeded, token_leader, token_member):
    r = await client.get("/api/export/my-teams.csv", headers=token_member)
    assert r.status_code == 403, "no managed teams, no scoped export"

    r = await client.get("/api/export/my-teams.csv", headers=token_leader)
    assert r.status_code == 200
    rows = _rows(r.content)[1:]
    assert {row[6] for row in rows} == {"Liturgy"}
    body = r.content.decode("utf-8-sig")
    assert "Maria" in body and "Lena" in body


async def test_leader_import_scoped_over_http(client, seeded, token_leader):
    # in scope: update Maria's phone and promote her on Liturgy
    content = _csv_bytes(
        [["", "Maria", "Alvarez", "maria@example.org", "555-42", "", "Liturgy", "core"]]
    )
    r = await client.post("/api/import", files=_upload(content), headers=token_leader)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["applied"] is True
    assert report["volunteers_updated"] == 1 and report["memberships_updated"] == 1

    # out of scope: a team the leader does not manage → row error, nothing applied
    async with db_session() as session:
        ok(await teams.create(session, None, "Hospitality"))
    content = _csv_bytes(
        [["", "Maria", "Alvarez", "maria@example.org", "", "", "Hospitality", "member"]]
    )
    r = await client.post("/api/import", files=_upload(content), headers=token_leader)
    assert r.status_code == 200
    report = r.json()
    assert report["applied"] is False
    assert any("not a team you lead" in e["message"] for e in report["errors"])


async def test_leader_dry_run_over_http(client, seeded, token_leader):
    content = _csv_bytes(
        [["", "Maria", "Alvarez", "maria@example.org", "555-77", "", "", ""]]
    )
    r = await client.post(
        "/api/import",
        params={"dry_run": "true"},
        files=_upload(content),
        headers=token_leader,
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["applied"] is False and report["volunteers_updated"] == 1
    assert not report["errors"]


async def test_import_dry_run_then_apply_over_http(
    client, seeded, token_admin, token_member
):
    content = _csv_bytes(
        [["", "Eve", "Green", "eve@example.org", "", "", "Liturgy", "leader"]]
    )

    r = await client.post("/api/import", files=_upload(content), headers=token_member)
    assert r.status_code == 403

    r = await client.post(
        "/api/import",
        params={"dry_run": "true"},
        files=_upload(content),
        headers=token_admin,
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


async def test_import_unreadable_file_reports_error(client, seeded, token_admin):
    r = await client.post(
        "/api/import", files=_upload(b"this is not a roster"), headers=token_admin
    )
    assert r.status_code == 200
    report = r.json()
    assert report["applied"] is False
    assert report["errors"] and "cannot identify CSV" in report["errors"][0]["message"]

    # xlsx uploads get a pointed message, not a parse attempt
    r = await client.post(
        "/api/import", files=_upload(b"PK\x03\x04 old workbook"), headers=token_admin
    )
    report = r.json()
    assert report["errors"]
    assert "no longer supported" in report["errors"][0]["message"]
