"""Roster CSV edge cases: injection escapes, date/role parsing, row validation."""

import csv
from datetime import date
from io import StringIO

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import teams, volunteers
from volunteerdb.sheets import exporter, importer
from volunteerdb.sheets.common import (
    ROSTER_HEADERS,
    clean_cell,
    parse_role,
    safe_cell,
)
from volunteerdb.sheets.importer import ImportReport, _parse_date


def _csv_bytes(rows: list[list], header: list[str] = ROSTER_HEADERS) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(StringIO(content.decode("utf-8-sig"))))


def test_clean_and_safe_units():
    assert safe_cell("=SUM(A1)") == "'=SUM(A1)"
    assert safe_cell("plain text") == "plain text"
    assert safe_cell(None) is None
    assert safe_cell(5) == 5, "non-strings pass through untouched"

    assert clean_cell("'=SUM(A1)") == "=SUM(A1)"
    assert clean_cell("  padded  ") == "padded"
    assert clean_cell("   ") is None
    assert clean_cell(None) is None
    assert clean_cell(safe_cell("=1+1")) == "=1+1", "escape/unescape are inverses"


def test_leading_formula_characters_are_escaped():
    """Excel and LibreOffice evaluate a cell opening with = + - or @, so an
    exported Canadian phone like '+1 416 555 0100' becomes a broken number the
    moment the parish opens the file."""
    for raw in ("=SUM(A1)", "+1 416 555 0100", "-5 min early", "@channel"):
        escaped = safe_cell(raw)
        assert escaped.startswith("'"), (
            f"{raw!r} would evaluate as a formula in a spreadsheet"
        )
        assert clean_cell(escaped) == raw, "escaping and cleaning must stay inverses"

    assert safe_cell("mid=equals") == "mid=equals", "only a leading trigger matters"


def test_parse_date_variants():
    report = ImportReport()
    assert _parse_date("2024-03-05", report, 4) == date(2024, 3, 5)
    assert _parse_date(None, report, 5) is None
    assert report.warnings == [], "no warnings so far"

    assert _parse_date("05/03/2024", report, 7) is None
    (warning,) = report.warnings
    assert warning.row == 7 and "unreadable date" in warning.message


def test_parse_role_value_and_label():
    assert parse_role("leader") == TeamRole.leader
    assert parse_role("Ministry leader") == TeamRole.leader
    assert parse_role("MEMBER") == TeamRole.member
    assert parse_role("Core team member") == TeamRole.core
    assert parse_role(" second ") == TeamRole.second
    assert parse_role("bogus") is None


async def test_formula_injection_escape_roundtrips(database):
    async with db_session() as session:
        await volunteers.create(
            session, "=Evil", "Person", "evil@example.org", notes="=SUM(A1:A9)"
        )
        content = await exporter.export_csv(session)

    row = next(r for r in _rows(content)[1:] if r[2] == "evil@example.org")
    assert row[0] == "'=Evil" and row[4] == "'=SUM(A1:A9)", (
        "cells never start with a bare ="
    )

    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors
    assert report.volunteers_updated == 0, "round-trip is a no-op"

    async with db_session() as session:
        (found,) = await volunteers.search(session, "evil@example.org")
        assert found.first_name == "=Evil" and found.notes == "=SUM(A1:A9)", (
            "database keeps the raw values"
        )


async def test_import_row_validation_errors(database):
    async with db_session() as session:
        await teams.create(session, "Liturgy")
        await volunteers.create(session, "Rhea", "Roleless", "rhea@example.org")

    content = _csv_bytes(
        [
            ["OnlyFirst", "", "", "", "", "yes", "", "", "", ""],
            ["Rhea", "Roleless", "rhea@example.org", "", "", "", "Liturgy", "", "", ""],
            ["Rhea", "Roleless", "rhea@example.org", "", "", "", "", "member", "", ""],
            [
                "Rhea",
                "Roleless",
                "rhea@example.org",
                "",
                "",
                "",
                "Liturgy",
                "grand-poobah",
                "",
                "",
            ],
        ]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)

    assert not report.applied, "all-or-nothing"
    messages = [issue.message for issue in report.errors]
    assert any("first and last name are both required" in m for m in messages)
    assert any("Role is required when Team is set" in m for m in messages)
    assert any("Team is required to assign a role" in m for m in messages)
    assert any("unknown role 'grand-poobah'" in m for m in messages)


async def test_import_ambiguous_matches_error(database):
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        youth = await teams.create(session, "Youth")
        await teams.create(session, "Music", parent_team_id=liturgy.id)
        await teams.create(session, "Music", parent_team_id=youth.id)
        await volunteers.create(session, "Sam", "Same")
        await volunteers.create(session, "Sam", "Same")
        await volunteers.create(session, "Uma", "Unique", "uma@example.org")

    content = _csv_bytes(
        [
            ["Sam", "Same", "", "", "", "", "Liturgy", "member", "", ""],
            ["Uma", "Unique", "uma@example.org", "", "", "", "Music", "member", "", ""],
        ]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)

    assert not report.applied
    messages = [issue.message for issue in report.errors]
    assert any("ambiguous: 2 volunteers match 'Sam Same'" in m for m in messages)
    assert any("'Music' is ambiguous, use its full path" in m for m in messages)


async def test_a_blank_cell_never_clears_an_existing_value(database):
    """Round-tripping an export and deleting a cell does NOT clear the field —
    only non-empty values are written back. Deliberate: a truncated paste would
    otherwise wipe contact details parish-wide, and all-or-nothing would not
    help because it is not an error."""
    async with db_session() as session:
        await volunteers.create(
            session,
            "Clara",
            "Contact",
            "clara@example.org",
            "555-0199",
            notes="sings alto",
        )

    content = _csv_bytes(
        [["Clara", "Contact", "clara@example.org", "", "", "yes", "", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_updated == 0, "nothing changed"

    async with db_session() as session:
        (found,) = await volunteers.search(session, "clara@example.org")
    assert found.phone == "555-0199" and found.notes == "sings alto", (
        "blanking a cell is a no-op; clearing a field needs the app, not the sheet"
    )


async def test_an_unrecognized_active_value_is_a_row_error(database):
    """Active is an allow-list both ways. Anything it does not recognise used to
    archive the volunteer with only a warning; a typo must block instead."""
    async with db_session() as session:
        await volunteers.create(session, "Vera", "Verbatim", "vera@example.org")

    content = _csv_bytes(
        [["Vera", "Verbatim", "vera@example.org", "", "", "Active", "", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.has_errors and not report.applied

    (error,) = report.errors
    assert error.row == 2 and "'Active'" in error.message
    assert "not recognised" in error.message

    async with db_session() as session:
        (found,) = await volunteers.search(session, "vera@example.org")
    assert found.is_active is True, "nothing was archived"


async def test_blank_active_leaves_an_archived_volunteer_archived(database):
    """A blank Active cell means 'leave unchanged' — it used to write True and
    silently un-archive people on every re-import."""
    async with db_session() as session:
        v = await volunteers.create(session, "Ana", "Archived", "ana@example.org")
        await volunteers.update(session, v.id, is_active=False)

    content = _csv_bytes(
        [["Ana", "Archived", "ana@example.org", "555-3", "", "", "", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied, report.errors

    async with db_session() as session:
        (found,) = await volunteers.search(
            session, "ana@example.org", include_inactive=True
        )
    assert found.phone == "555-3", "the row still applied"
    assert found.is_active is False, "blank Active must not re-activate"


async def test_blank_active_on_a_new_volunteer_defaults_to_active(database):
    content = _csv_bytes(
        [["Blank", "Active", "blank@example.org", "", "", "", "", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_created == 1

    async with db_session() as session:
        (found,) = await volunteers.search(session, "blank@example.org")
        assert found.is_active is True


async def test_active_no_archives_and_yes_reactivates(database):
    async with db_session() as session:
        await volunteers.create(session, "Tess", "Toggle", "tess@example.org")

    content = _csv_bytes(
        [["Tess", "Toggle", "tess@example.org", "", "", "no", "", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_updated == 1
    async with db_session() as session:
        (found,) = await volunteers.search(
            session, "tess@example.org", include_inactive=True
        )
        assert found.is_active is False

    content = _csv_bytes(
        [["Tess", "Toggle", "tess@example.org", "", "", "yes", "", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_updated == 1
    async with db_session() as session:
        (found,) = await volunteers.search(session, "tess@example.org")
        assert found.is_active is True


async def test_a_whole_file_of_unreadable_dates_still_applies(database):
    """An unreadable Joined on is a warning, not an error, and has_errors only
    looks at errors — so a file whose every date is in the wrong format imports
    'successfully' with every join date dropped."""
    async with db_session() as session:
        await teams.create(session, "Liturgy")
        for n in range(3):
            await volunteers.create(session, f"V{n}", "Dated", f"v{n}@example.org")

    content = _csv_bytes(
        [
            [
                f"V{n}",
                "Dated",
                f"v{n}@example.org",
                "",
                "",
                "",
                "Liturgy",
                "member",
                "03/05/2026",
                "",
            ]
            for n in range(3)
        ]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)

    assert report.applied, "warnings do not block the import"
    assert report.memberships_created == 3
    assert len(report.warnings) == 3
    assert all("unreadable date" in w.message for w in report.warnings)

    async with db_session() as session:
        (v0,) = await volunteers.search(session, "V0")
        rows = await volunteers.assignments(session, v0.id)
    assert rows and all(m.joined_on is None for m, _team in rows), (
        "every join date was silently dropped"
    )
