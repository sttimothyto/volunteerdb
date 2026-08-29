"""Roster CSV edge cases: injection escapes, role parsing, row validation."""

import csv
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

from tests.fp_helpers import ok


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


def test_parse_role_value_and_label():
    assert parse_role("leader") == TeamRole.leader
    assert parse_role("Ministry leader") == TeamRole.leader
    assert parse_role("MEMBER") == TeamRole.member
    assert parse_role("Core team member") == TeamRole.core
    assert parse_role(" second ") == TeamRole.second
    assert parse_role("bogus") is None


async def test_formula_injection_escape_roundtrips(database):
    async with db_session() as session:
        ok(
            await volunteers.create(
                session,
                None,
                "=Evil",
                "Person",
                "evil@example.org",
                notes="=SUM(A1:A9)",
            )
        )
        content = await exporter.export_csv(session, None)

    row = next(r for r in _rows(content)[1:] if r[3] == "evil@example.org")
    assert row[1] == "'=Evil" and row[5] == "'=SUM(A1:A9)", (
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
        ok(await teams.create(session, None, "Liturgy"))
        ok(
            await volunteers.create(
                session, None, "Rhea", "Roleless", "rhea@example.org"
            )
        )

    content = _csv_bytes(
        [
            ["", "OnlyFirst", "", "", "", "", "", ""],
            ["", "Rhea", "Roleless", "rhea@example.org", "", "", "Liturgy", ""],
            ["", "Rhea", "Roleless", "rhea@example.org", "", "", "", "member"],
            [
                "",
                "Rhea",
                "Roleless",
                "rhea@example.org",
                "",
                "",
                "Liturgy",
                "grand-poobah",
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
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        youth = ok(await teams.create(session, None, "Youth"))
        ok(await teams.create(session, None, "Music", parent_team_id=liturgy.id))
        ok(await teams.create(session, None, "Music", parent_team_id=youth.id))
        ok(await volunteers.create(session, None, "Sam", "Same"))
        ok(await volunteers.create(session, None, "Sam", "Same"))
        ok(await volunteers.create(session, None, "Uma", "Unique", "uma@example.org"))

    content = _csv_bytes(
        [
            ["", "Sam", "Same", "", "", "", "Liturgy", "member"],
            ["", "Uma", "Unique", "uma@example.org", "", "", "Music", "member"],
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
        ok(
            await volunteers.create(
                session,
                None,
                "Clara",
                "Contact",
                "clara@example.org",
                "555-0199",
                notes="sings alto",
            )
        )

    content = _csv_bytes(
        [["", "Clara", "Contact", "clara@example.org", "", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_updated == 0, "nothing changed"

    async with db_session() as session:
        (found,) = await volunteers.search(session, "clara@example.org")
    assert found.phone == "555-0199" and found.notes == "sings alto", (
        "blanking a cell is a no-op; clearing a field needs the app, not the sheet"
    )


async def test_new_volunteers_are_created_active(database):
    content = _csv_bytes([["", "Newly", "Active", "newly@example.org", "", "", "", ""]])
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied and report.volunteers_created == 1

    async with db_session() as session:
        (found,) = await volunteers.search(session, "newly@example.org")
        assert found.is_active is True


async def test_import_cannot_archive_anyone(database):
    """There is no Active column: archiving happens in the app (or when a sync
    removes someone's last membership), never from a spreadsheet cell."""
    async with db_session() as session:
        ok(
            await volunteers.create(
                session, None, "Vera", "Verbatim", "vera@example.org"
            )
        )

    content = _csv_bytes(
        [["", "Vera", "Verbatim", "vera@example.org", "555-7", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied, report.errors

    async with db_session() as session:
        (found,) = await volunteers.search(session, "vera@example.org")
    assert found.phone == "555-7" and found.is_active is True


async def test_volunteer_only_row_does_not_reactivate(database):
    """Only a row that puts an archived volunteer on a team reactivates them;
    a bare contact update leaves the archive flag alone."""
    async with db_session() as session:
        v = ok(
            await volunteers.create(session, None, "Ana", "Archived", "ana@example.org")
        )
        ok(await volunteers.update(session, None, v.id, is_active=False))

    content = _csv_bytes(
        [["", "Ana", "Archived", "ana@example.org", "555-3", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.applied, report.errors
    assert report.volunteers_reactivated == 0

    async with db_session() as session:
        (found,) = await volunteers.search(
            session, "ana@example.org", include_inactive=True
        )
    assert found.phone == "555-3", "the row still applied"
    assert found.is_active is False, "a contact update must not re-activate"
