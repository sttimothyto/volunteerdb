"""How the importer decides that a spreadsheet row *is* an existing volunteer.

This is the rule that produced duplicates during the July 2026 parish import
(see ingest-data/ingest_july.py, whose IDENTITY_LINKS table exists purely to
work around it). Matching is email-first: a row carrying an email is looked up
by email and nothing else, so a new address on an existing person creates a
second record rather than updating the first. The name index is consulted only
when the email cell is blank.
"""

from io import BytesIO

from openpyxl import load_workbook

from volunteerdb.db import db_session
from volunteerdb.services import teams, volunteers
from volunteerdb.sheets import exporter, importer
from volunteerdb.sheets.common import MEMBERSHIP_SHEET, VOLUNTEER_SHEET


def _workbook_bytes(volunteer_rows: list = (), membership_rows: list = ()) -> bytes:
    wb = load_workbook(BytesIO(exporter.template_workbook()))
    for row in volunteer_rows:
        wb[VOLUNTEER_SHEET].append(row)
    for row in membership_rows:
        wb[MEMBERSHIP_SHEET].append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def test_unmatched_email_does_not_fall_back_to_name(database):
    """A volunteer with no email on file, plus a sheet row that names them AND
    supplies an address, yields two records. Intentional — see
    docs/reference/spreadsheets.md — but it is the July duplicate in miniature."""
    async with db_session() as session:
        await volunteers.create(session, "Andrea", "Smart")

    content = _workbook_bytes(
        volunteer_rows=[
            ["Andrea", "Smart", "a.smart705@outlook.com", None, None, "yes"]
        ]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_created == 1
    assert report.volunteers_updated == 0

    async with db_session() as session:
        found = await volunteers.search(session, "Andrea Smart")
    assert len(found) == 2, "email-first matching never falls back to the name index"
    assert sorted(v.email or "" for v in found) == ["", "a.smart705@outlook.com"]


async def test_a_blank_email_still_matches_by_name(database):
    """The contrast that makes the rule comprehensible: with no email in the
    cell, an exact full-name match updates the existing volunteer."""
    async with db_session() as session:
        await volunteers.create(session, "Andrea", "Smart")

    content = _workbook_bytes(
        volunteer_rows=[["Andrea", "Smart", None, "555-0143", None, "yes"]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied
    assert report.volunteers_created == 0 and report.volunteers_updated == 1

    async with db_session() as session:
        (found,) = await volunteers.search(session, "Andrea Smart")
    assert found.phone == "555-0143"


async def test_new_email_on_an_existing_name_warns_before_duplicating(database):
    """Creating the duplicate is the documented behaviour; doing it silently is
    what cost a day of cleanup. The report must say so."""
    async with db_session() as session:
        await volunteers.create(session, "Andrea", "Smart")
        await volunteers.create(session, "Bruno", "Newcomer")

    content = _workbook_bytes(
        volunteer_rows=[
            ["Andrea", "Smart", "a.smart705@outlook.com", None, None, "yes"],
            ["Cora", "Fresh", "cora@example.org", None, None, "yes"],
        ]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_created == 2
    assert not report.has_errors, "a warning must not block the import"

    (warning,) = report.warnings
    assert warning.sheet == VOLUNTEER_SHEET and warning.row == 2
    assert "a.smart705@outlook.com" in warning.message
    assert "Andrea Smart" in warning.message
    assert "already exists" in warning.message, (
        "the operator needs to be told which existing person this row may be"
    )


async def test_family_shared_email_is_disambiguated_by_name(database):
    """Two people on one address is normal in a parish. The name breaks the tie;
    without a usable name the row is an error rather than a coin flip."""
    async with db_session() as session:
        await teams.create(session, "Liturgy")
        await volunteers.create(session, "Maria", "Alvarez", "family@example.org")
        await volunteers.create(session, "Jose", "Alvarez", "family@example.org")

    content = _workbook_bytes(
        volunteer_rows=[
            ["Maria", "Alvarez", "family@example.org", "555-0100", None, "yes"]
        ]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied, [i.message for i in report.errors]
    assert report.volunteers_created == 0 and report.volunteers_updated == 1

    async with db_session() as session:
        found = {
            v.first_name: v.phone for v in await volunteers.search(session, "Alvarez")
        }
    assert found == {"Maria": "555-0100", "Jose": None}, "only the named spouse changed"

    ambiguous = _workbook_bytes(
        membership_rows=[["family@example.org", None, "Liturgy", "member", None, None]]
    )
    report = await importer.run_import(ambiguous, dry_run=False, user_id=None)
    assert not report.applied
    assert any("ambiguous: 2 volunteers match" in i.message for i in report.errors)
