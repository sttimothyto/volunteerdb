"""CSV variant: two single-sheet files, header auto-detect, Excel-safe output."""

import csv
from io import StringIO

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, volunteers
from volunteerdb.sheets import exporter, importer
from volunteerdb.sheets.common import MEMBERSHIP_HEADERS, VOLUNTEER_HEADERS


def _csv_bytes(header, rows) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(StringIO(content.decode("utf-8-sig"))))


async def _setup(session):
    liturgy = await teams.create(session, "Liturgy")
    anna = await volunteers.create(session, "Anna", "Smith", "anna@example.org", phone="555-1")
    await memberships.assign(session, anna.id, liturgy.id, TeamRole.leader)
    return liturgy, anna


async def test_template_csv_headers_and_bom():
    for sheet, headers in (("volunteers", VOLUNTEER_HEADERS), ("memberships", MEMBERSHIP_HEADERS)):
        content = exporter.template_csv(sheet)
        assert content.startswith(b"\xef\xbb\xbf"), "BOM so Excel detects UTF-8"
        assert _rows(content) == [headers]


async def test_csv_export_reimports_as_noop(database):
    async with db_session() as session:
        await _setup(session)
        vol_csv = await exporter.export_csv(session, "volunteers")
        mem_csv = await exporter.export_csv(session, "memberships")

    assert _rows(vol_csv)[0] == VOLUNTEER_HEADERS
    assert _rows(mem_csv)[0] == MEMBERSHIP_HEADERS
    for content in (vol_csv, mem_csv):
        report = await importer.run_import(content, dry_run=False, user_id=None)
        assert not report.has_errors, report.errors
        assert report.applied
        assert report.volunteers_created == report.volunteers_updated == 0
        assert report.memberships_created == report.memberships_updated == 0


async def test_csv_imports_add_and_update(database):
    async with db_session() as session:
        await _setup(session)

    content = _csv_bytes(
        VOLUNTEER_HEADERS, [["Cara", "White", "cara@example.org", "555-9", "", "yes"]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied and report.volunteers_created == 1

    content = _csv_bytes(
        MEMBERSHIP_HEADERS, [["cara@example.org", "Cara White", "Liturgy", "member", "2024-05-03", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied and report.memberships_created == 1

    async with db_session() as session:
        (cara,) = await volunteers.search(session, "Cara")
        ((membership, _),) = await volunteers.assignments(session, cara.id)
        assert membership.joined_on.isoformat() == "2024-05-03"


async def test_csv_unrecognized_header_rejected(database):
    report = await importer.run_import(b"foo,bar\n1,2\n", dry_run=False, user_id=None)
    assert report.has_errors and not report.applied
    assert "cannot identify CSV" in report.errors[0].message


async def test_csv_non_utf8_rejected(database):
    report = await importer.run_import(b"\xc3\x28\xa0\xa1", dry_run=False, user_id=None)
    assert report.has_errors and not report.applied
    assert "cannot read file" in report.errors[0].message


async def test_csv_formula_injection_escaped_and_roundtrips(database):
    async with db_session() as session:
        liturgy, anna = await _setup(session)
        await volunteers.update(session, anna.id, notes="=SUM(A1:A9)")
        vol_csv = await exporter.export_csv(session, "volunteers")

    anna_row = next(r for r in _rows(vol_csv)[1:] if r[0] == "Anna")
    assert anna_row[4] == "'=SUM(A1:A9)", "escaped so Excel shows text, not a formula"

    report = await importer.run_import(vol_csv, dry_run=False, user_id=None)
    assert not report.has_errors and report.volunteers_updated == 0
    async with db_session() as session:
        (found,) = await volunteers.search(session, "Anna")
        assert found.notes == "=SUM(A1:A9)", "the escape is stripped on import"


async def test_csv_unreadable_date_warns_and_blank_active_is_active(database):
    async with db_session() as session:
        await _setup(session)

    content = _csv_bytes(
        VOLUNTEER_HEADERS, [["Cara", "White", "cara@example.org", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors, report.errors
    async with db_session() as session:
        (cara,) = await volunteers.search(session, "Cara")
        assert cara.is_active, "blank Active means active, as in xlsx"

    content = _csv_bytes(
        MEMBERSHIP_HEADERS, [["cara@example.org", "Cara White", "Liturgy", "member", "05/03/2024", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors and report.applied
    assert any("unreadable date" in w.message for w in report.warnings)


async def test_csv_extra_volunteer_columns_warn(database):
    async with db_session() as session:
        await _setup(session)
    content = _csv_bytes(
        [*VOLUNTEER_HEADERS, "Shirt size"],
        [["Anna", "Smith", "anna@example.org", "555-1", "", "yes", "M"]],
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors, report.errors
    assert any("custom" in w.message for w in report.warnings)
