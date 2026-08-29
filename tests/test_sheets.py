"""Roster CSV export → import round-trip, validation, and dry-run."""

import csv
from io import StringIO

from volunteerdb.db import db_session
from volunteerdb.models import FieldType, TeamRole
from volunteerdb.services import custom_fields, memberships, teams, volunteers
from volunteerdb.sheets import exporter, importer
from volunteerdb.sheets.common import ROSTER_HEADERS

from tests.fp_helpers import ok


def _csv_bytes(rows: list[list], header: list[str] = ROSTER_HEADERS) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(StringIO(content.decode("utf-8-sig"))))


async def _setup(session):
    liturgy = ok(await teams.create(session, None, "Liturgy"))
    music = ok(await teams.create(session, None, "Music", parent_team_id=liturgy.id))
    anna = await volunteers.create(
        session, None, "Anna", "Smith", "anna@example.org", phone="555-1"
    )
    ben = await volunteers.create(session, None, "Ben", "Jones", "ben@example.org")
    await memberships.assign(session, None, anna.id, liturgy.id, TeamRole.leader)
    await memberships.assign(session, None, ben.id, music.id, TeamRole.member)
    return liturgy, music, anna, ben


async def test_template_csv_header_and_bom():
    content = exporter.template_csv()
    assert content.startswith(b"\xef\xbb\xbf"), "BOM so Excel detects UTF-8"
    assert _rows(content) == [ROSTER_HEADERS]


async def test_roundtrip_reimport_is_a_noop(database):
    async with db_session() as session:
        await _setup(session)
        # an unassigned volunteer exercises the blank-Team parish rows too
        await volunteers.create(session, None, "Ursula", "Unassigned", "u@example.org")
        content = await exporter.export_csv(session, None)

    assert _rows(content)[0] == ROSTER_HEADERS
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.volunteers_created == 0
    assert report.volunteers_updated == 0
    assert report.memberships_created == 0
    assert report.memberships_updated == 0


async def test_import_applies_edits_and_additions(database):
    async with db_session() as session:
        await _setup(session)
        content = await exporter.export_csv(session, None)

    rows = _rows(content)
    # promote Ben to Music leader (Role is the last column of his row)
    for row in rows[1:]:
        if row[3] == "ben@example.org":
            row[7] = "Ministry leader"
    # add a brand-new volunteer (blank ID) with a membership by team path
    rows.append(
        [
            "",
            "Cara",
            "White",
            "cara@example.org",
            "555-9",
            "",
            "Liturgy / Music",
            "member",
        ]
    )

    report = await importer.run_import(
        _csv_bytes(rows[1:], header=rows[0]), dry_run=False, user_id=None
    )
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.volunteers_created == 1
    assert report.memberships_created == 1
    assert report.memberships_updated == 1

    async with db_session() as session:
        cara = (await volunteers.search(session, "Cara"))[0]
        assert cara.email == "cara@example.org"
        found = await volunteers.search(session, "Ben")
        ben_assignments = await volunteers.assignments(session, found[0].id)
        assert ben_assignments[0][0].role == TeamRole.leader


async def test_parish_export_lists_unassigned_after_memberships(database):
    async with db_session() as session:
        await _setup(session)
        await volunteers.create(session, None, "Ursula", "Unassigned", "u@example.org")
        content = await exporter.export_csv(session, None)

    rows = _rows(content)[1:]
    assert rows[-1][1] == "Ursula" and rows[-1][6] == "", (
        "membership-less volunteers come last, with blank team columns"
    )
    assert all(r[6] for r in rows[:-1]), "every other row carries its team path"
    assert all(r[0].isdigit() for r in rows), "every row carries the volunteer's ID"


async def test_parish_export_omits_archived_unassigned_but_keeps_members(database):
    """Without an Active column an archived row would be indistinguishable from
    a live one, so the unassigned tail lists active volunteers only. Archived
    volunteers still holding a membership stay in: their rows are what keeps a
    team-sheet round-trip from reading them as 'removed'."""
    async with db_session() as session:
        _, _, anna, _ = await _setup(session)
        gone = await volunteers.create(
            session, None, "Gone", "Quietly", "gone@example.org"
        )
        await volunteers.update(session, None, gone.id, is_active=False)
        await volunteers.update(
            session, None, anna.id, is_active=False
        )  # keeps membership
        content = await exporter.export_csv(session, None)

    body = content.decode("utf-8-sig")
    assert "gone@example.org" not in body, "archived + membership-less: omitted"
    assert "anna@example.org" in body, "archived but on a team: still exported"


async def test_unknown_team_blocks_everything(database):
    async with db_session() as session:
        await _setup(session)

    content = _csv_bytes(
        [
            [
                "",
                "Dave",
                "Black",
                "dave@example.org",
                "",
                "",
                "No Such Team",
                "member",
            ]
        ]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert report.has_errors
    assert not report.applied
    assert "No Such Team" in report.errors[0].message

    async with db_session() as session:
        assert await volunteers.search(session, "Dave") == [], (
            "all-or-nothing: Dave not created"
        )


async def test_dry_run_writes_nothing(database):
    async with db_session() as session:
        ok(await teams.create(session, None, "Liturgy"))

    content = _csv_bytes(
        [
            [
                "",
                "Eve",
                "Green",
                "eve@example.org",
                "",
                "",
                "Liturgy",
                "leader",
            ]
        ]
    )
    report = await importer.run_import(content, dry_run=True, user_id=None)
    assert not report.has_errors
    assert not report.applied
    assert report.volunteers_created == 1  # would be created

    async with db_session() as session:
        assert await volunteers.search(session, "Eve") == []


async def test_volunteer_only_row_needs_no_team(database):
    async with db_session() as session:
        await _setup(session)

    content = _csv_bytes(
        [["", "Cara", "White", "cara@example.org", "555-9", "", "", ""]]
    )
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied and report.volunteers_created == 1
    assert report.memberships_created == 0


async def test_xlsx_upload_rejected_with_pointer_to_csv(database):
    report = await importer.run_import(
        b"PK\x03\x04 pretend workbook", dry_run=False, user_id=None
    )
    assert report.has_errors and not report.applied
    assert "no longer supported" in report.errors[0].message


async def test_unrecognized_header_rejected(database):
    report = await importer.run_import(b"foo,bar\n1,2\n", dry_run=False, user_id=None)
    assert report.has_errors and not report.applied
    assert "cannot identify CSV" in report.errors[0].message


async def test_non_utf8_rejected(database):
    report = await importer.run_import(b"\xc3\x28\xa0\xa1", dry_run=False, user_id=None)
    assert report.has_errors and not report.applied
    assert "cannot read file" in report.errors[0].message


async def test_export_includes_custom_columns_and_reimport_ignores_them(database):
    async with db_session() as session:
        _, _, anna, _ = await _setup(session)
        await custom_fields.create_def(session, None, "Shirt size", FieldType.text)
        await custom_fields.create_def(session, None, "Trained", FieldType.checkbox)
        await custom_fields.create_def(session, None, "Term", FieldType.interval)
        await custom_fields.set_values(
            session,
            None,
            anna.id,
            {"shirt_size": "M", "trained": True, "term": "P1DT2H"},
        )
        content = await exporter.export_csv(session, None)

    rows = _rows(content)
    assert rows[0][-3:] == ["Shirt size", "Term", "Trained"]
    anna_row = next(r for r in rows[1:] if r[1] == "Anna")
    assert anna_row[8] == "M" and anna_row[9] == "P1DT2H" and anna_row[10] == "yes"

    # round-trip stays safe: the extra columns are ignored with a warning
    report = await importer.run_import(content, dry_run=False, user_id=None)
    assert not report.has_errors
    assert report.applied
    assert report.volunteers_updated == 0
    assert any("custom" in w.message for w in report.warnings)

    async with db_session() as session:
        (found,) = await volunteers.search(session, "Anna")
        assert found.custom == {
            "shirt_size": "M",
            "trained": True,
            "term": "P1DT2H",
        }, "values untouched"
