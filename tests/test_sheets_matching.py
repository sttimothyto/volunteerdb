"""How the importer decides that a spreadsheet row *is* an existing volunteer.

A row carrying an ID (exports always write one) is pinned to that exact
record; matching only starts when the ID cell is blank. Blank-ID matching is
the rule that produced duplicates during the July 2026 parish import (see
ingest-data/ingest_july.py, whose IDENTITY_LINKS table exists purely to work
around it): it is email-first — a row carrying an email is looked up by email
and nothing else, so a new address on an existing person creates a second
record rather than updating the first. The name index is consulted only when
the email cell is blank.
"""

import csv
from io import StringIO

from volunteerdb.db import db_session
from volunteerdb.services import teams, volunteers
from volunteerdb.sheets import importer
from volunteerdb.sheets.common import ROSTER_HEADERS, ROSTER_SHEET

from tests.fp_helpers import ok


def _csv_bytes(rows: list[list]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ROSTER_HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


async def test_unmatched_email_does_not_fall_back_to_name(database, env):
    """A volunteer with no email on file, plus a sheet row that names them AND
    supplies an address, yields two records. Intentional — see
    docs/reference/spreadsheets.md — but it is the July duplicate in miniature."""
    async with db_session() as session:
        ok(await volunteers.create(session, None, "Andrea", "Smart"))

    content = _csv_bytes(
        [["", "Andrea", "Smart", "a.smart705@outlook.com", "", "", "", ""]]
    )
    report = ok(await importer.run_import(env, content, dry_run=False, user_id=None))
    assert report.applied and report.volunteers_created == 1
    assert report.volunteers_updated == 0

    async with db_session() as session:
        found = await volunteers.search(session, "Andrea Smart")
    assert len(found) == 2, "email-first matching never falls back to the name index"
    assert sorted(v.email or "" for v in found) == ["", "a.smart705@outlook.com"]


async def test_a_blank_email_still_matches_by_name(database, env):
    """The contrast that makes the rule comprehensible: with no email in the
    cell, an exact full-name match updates the existing volunteer."""
    async with db_session() as session:
        ok(await volunteers.create(session, None, "Andrea", "Smart"))

    content = _csv_bytes([["", "Andrea", "Smart", "", "555-0143", "", "", ""]])
    report = ok(await importer.run_import(env, content, dry_run=False, user_id=None))
    assert report.applied
    assert report.volunteers_created == 0 and report.volunteers_updated == 1

    async with db_session() as session:
        (found,) = await volunteers.search(session, "Andrea Smart")
    assert found.phone == "555-0143"


async def test_new_email_on_an_existing_name_warns_before_duplicating(database, env):
    """Creating the duplicate is the documented behaviour; doing it silently is
    what cost a day of cleanup. The report must say so."""
    async with db_session() as session:
        ok(await volunteers.create(session, None, "Andrea", "Smart"))
        ok(await volunteers.create(session, None, "Bruno", "Newcomer"))

    content = _csv_bytes(
        [
            ["", "Andrea", "Smart", "a.smart705@outlook.com", "", "", "", ""],
            ["", "Cora", "Fresh", "cora@example.org", "", "", "", ""],
        ]
    )
    report = ok(await importer.run_import(env, content, dry_run=False, user_id=None))
    assert report.applied and report.volunteers_created == 2
    assert not report.has_errors, "a warning must not block the import"

    (warning,) = report.warnings
    assert warning.sheet == ROSTER_SHEET and warning.row == 2
    assert "a.smart705@outlook.com" in warning.message
    assert "Andrea Smart" in warning.message
    assert "already exists" in warning.message, (
        "the operator needs to be told which existing person this row may be"
    )


async def test_family_shared_email_is_disambiguated_by_name(database, env):
    """Two people on one address is normal in a parish. The name breaks the tie;
    without a usable name the row is an error rather than a coin flip."""
    async with db_session() as session:
        ok(await teams.create(session, None, "Liturgy"))
        ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "family@example.org"
            )
        )
        ok(
            await volunteers.create(
                session, None, "Jose", "Alvarez", "family@example.org"
            )
        )

    content = _csv_bytes(
        [["", "Maria", "Alvarez", "family@example.org", "555-0100", "", "", ""]]
    )
    report = ok(await importer.run_import(env, content, dry_run=False, user_id=None))
    assert report.applied, [i.message for i in report.errors]
    assert report.volunteers_created == 0 and report.volunteers_updated == 1

    async with db_session() as session:
        found = {
            v.first_name: v.phone for v in await volunteers.search(session, "Alvarez")
        }
    assert found == {"Maria": "555-0100", "Jose": None}, "only the named spouse changed"

    # both spouses share the email AND the surname-only name cannot break the
    # tie for a row that names neither exactly — force the ambiguity with a
    # same-named third record
    async with db_session() as session:
        ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "family@example.org"
            )
        )
    ambiguous = _csv_bytes(
        [["", "Maria", "Alvarez", "family@example.org", "", "", "Liturgy", "member"]]
    )
    report = ok(await importer.run_import(env, ambiguous, dry_run=False, user_id=None))
    assert not report.applied
    assert any("ambiguous: 2 volunteers match" in i.message for i in report.errors)


async def test_id_pins_the_row_and_makes_email_edits_safe(database, env):
    """The whole point of the ID column: with the row pinned, a new address is
    a correction, not a new person — and later blank-ID rows carrying the new
    address must find the corrected record instead of duplicating it."""
    async with db_session() as session:
        maria = ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "maria.old@example.org"
            )
        )
        maria_id = maria.id

    content = _csv_bytes(
        [
            [
                str(maria_id),
                "Maria",
                "Alvarez",
                "maria.new@example.org",
                "",
                "",
                "",
                "",
            ],
            ["", "Maria", "Alvarez", "maria.new@example.org", "555-0177", "", "", ""],
        ]
    )
    report = ok(await importer.run_import(env, content, dry_run=False, user_id=None))
    assert report.applied, [i.message for i in report.errors]
    assert report.volunteers_created == 0, "no duplicate from either row"
    assert report.volunteers_updated == 2, "both rows landed on the same record"

    async with db_session() as session:
        found = await volunteers.search(session, "Maria Alvarez")
    assert len(found) == 1
    assert found[0].email == "maria.new@example.org" and found[0].phone == "555-0177"


async def test_id_takes_precedence_over_a_conflicting_email(database, env):
    async with db_session() as session:
        maria = ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "maria@example.org"
            )
        )
        ok(
            await volunteers.create(
                session, None, "Jose", "Alvarez", "jose@example.org"
            )
        )
        maria_id = maria.id

    # the email cell says Jose, the ID says Maria — the ID wins
    content = _csv_bytes(
        [[str(maria_id), "Maria", "Alvarez", "jose@example.org", "", "", "", ""]]
    )
    report = ok(await importer.run_import(env, content, dry_run=False, user_id=None))
    assert report.applied and report.volunteers_created == 0

    async with db_session() as session:
        found = {
            v.first_name: v.email for v in await volunteers.search(session, "Alvarez")
        }
    assert found["Maria"] == "jose@example.org", "Maria's record took the row"
    assert found["Jose"] == "jose@example.org", "Jose untouched"


async def test_unknown_or_malformed_id_is_a_row_error(database, env):
    content = _csv_bytes(
        [
            ["999999", "Ghost", "Record", "ghost@example.org", "", "", "", ""],
            ["twelve", "Word", "Number", "word@example.org", "", "", "", ""],
        ]
    )
    report = ok(await importer.run_import(env, content, dry_run=False, user_id=None))
    assert report.has_errors and not report.applied
    messages = [i.message for i in report.errors]
    assert any("matches no volunteer" in m for m in messages)
    assert any("is not a number" in m for m in messages)

    async with db_session() as session:
        assert await volunteers.search(session, "Ghost") == [], (
            "an unknown ID never creates a volunteer"
        )


async def test_stale_id_with_a_different_name_warns(database, env):
    """A copy-pasted row keeping the old ID silently rewrites an unrelated
    volunteer — both names differing is the tell. A surname change alone
    (marriage) stays quiet."""
    async with db_session() as session:
        maria = ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "maria@example.org"
            )
        )
        maria_id = maria.id

    content = _csv_bytes(
        [[str(maria_id), "Bob", "Smith", "bob@example.org", "", "", "", ""]]
    )
    report = ok(await importer.run_import(env, content, dry_run=True, user_id=None))
    assert not report.has_errors
    (warning,) = report.warnings
    assert "'Maria Alvarez'" in warning.message and "'Bob Smith'" in warning.message

    renamed = _csv_bytes(
        [[str(maria_id), "Maria", "Smith", "maria@example.org", "", "", "", ""]]
    )
    report = ok(await importer.run_import(env, renamed, dry_run=True, user_id=None))
    assert report.warnings == [], "a surname-only change does not warn"
