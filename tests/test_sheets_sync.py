"""Sync mode: a team's sheet, applied as that team's roster.

The contract the whole roster-spreadsheet feature rests on: a sheet adds and
updates, and never removes. A leader who deletes a row has not fired anybody,
and the sync's write-back leg puts that person straight back into the sheet.
Memberships end in the app.

What sync mode still changes, and what these tests are mostly about: blank
Team cells default to the sheet's own team, rows naming any other team are
errors, and the sheet may not rewrite the contact details of anyone not
already on the roster.
"""

import csv
from io import StringIO

import pytest
import sqlalchemy as sa

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole, membership_history
from volunteerdb.services import memberships, teams, volunteers
from volunteerdb.sheets import exporter, importer
from volunteerdb.sheets.common import ROSTER_HEADERS

from tests.fp_helpers import ok


def _csv_bytes(rows: list[list]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ROSTER_HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


@pytest.fixture
async def choir(database):
    """Choir with Lena (leader), Mia and Carl (members); Carl also serves on
    Hospitality. Dora is on Choir only."""
    async with db_session() as session:
        choir = ok(await teams.create(session, None, "Choir"))
        hospitality = ok(await teams.create(session, None, "Hospitality"))
        lena = ok(
            await volunteers.create(session, None, "Lena", "Leader", "lena@example.org")
        )
        mia = ok(
            await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
        )
        carl = ok(
            await volunteers.create(session, None, "Carl", "Cross", "carl@example.org")
        )
        dora = ok(
            await volunteers.create(session, None, "Dora", "Done", "dora@example.org")
        )
        ok(await memberships.assign(session, None, lena.id, choir.id, TeamRole.leader))
        ok(await memberships.assign(session, None, mia.id, choir.id, TeamRole.member))
        ok(await memberships.assign(session, None, carl.id, choir.id, TeamRole.member))
        ok(
            await memberships.assign(
                session, None, carl.id, hospitality.id, TeamRole.member
            )
        )
        ok(await memberships.assign(session, None, dora.id, choir.id, TeamRole.member))
        return {
            "choir": choir.id,
            "hospitality": hospitality.id,
            "lena": lena.id,
            "mia": mia.id,
            "carl": carl.id,
            "dora": dora.id,
        }


async def _team_volunteer_ids(team_id: int) -> set[int]:
    async with db_session() as session:
        pairs = ok(await teams.roster(session, None, team_id))
        return {volunteer.id for _, volunteer in pairs}


async def test_sync_roundtrip_of_team_export_is_a_noop(choir):
    async with db_session() as session:
        content = await exporter.export_csv(
            session, None, team_id=choir["choir"], subtree=False
        )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.volunteers_created == report.volunteers_updated == 0
    assert report.memberships_created == report.memberships_updated == 0


async def test_sync_applies_adds_and_updates_but_removes_nobody(choir):
    """Lena unchanged, Mia promoted, Nora added; Carl and Dora simply absent.
    Absence is not a departure — they keep their memberships, and the caller's
    write-back leg is what puts them back into the sheet."""
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "core"],
            ["", "Nora", "New", "nora@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.volunteers_created == 1
    assert report.memberships_created == 1
    assert report.memberships_updated == 1

    roster = await _team_volunteer_ids(choir["choir"])
    assert choir["carl"] in roster and choir["dora"] in roster

    async with db_session() as session:
        (dora,) = await volunteers.search(session, "dora@example.org")
        assert dora.is_active, "nobody is archived by a sync any more"
        # and no membership was ever deleted, so the history twin has no D row
        deleted = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(membership_history)
                .where(membership_history.c.op == "D")
            )
        ).scalar_one()
        assert deleted == 0


async def test_sync_blank_team_defaults_to_the_sheet_team(choir):
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "", "member"],
            ["", "Dora", "Done", "dora@example.org", "", "", "", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied


async def test_sync_rejects_rows_for_other_teams(choir):
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Hospitality", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
            ["", "Dora", "Done", "dora@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert report.has_errors and not report.applied
    assert any("only manages 'Choir'" in e.message for e in report.errors)
    assert await _team_volunteer_ids(choir["choir"]) == {
        choir["lena"],
        choir["mia"],
        choir["carl"],
        choir["dora"],
    }, "all-or-nothing per team"


async def test_a_row_error_rolls_the_whole_team_back(choir):
    """Mia's Role cell is garbage. All-or-nothing per team: the good rows in
    the same file must not land either."""
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "oui"],
            ["", "Nora", "New", "nora@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert report.has_errors and not report.applied
    # the report still COUNTS what it would have done — the rollback is the
    # database's, not the tally's — so the state is what the assertion checks
    async with db_session() as session:
        assert await volunteers.search(session, "nora@example.org") == []


async def test_sync_dry_run_reports_without_writing(choir):
    content = _csv_bytes(
        [["", "Nora", "New", "nora@example.org", "", "", "Choir", "member"]]
    )
    report = await importer.run_team_sync(
        content, team_id=choir["choir"], user_id=None, dry_run=True
    )
    assert not report.has_errors and not report.applied
    assert report.volunteers_created == 1, "reported..."
    async with db_session() as session:
        assert await volunteers.search(session, "nora@example.org") == [], (
            "...but rolled back"
        )


async def test_sync_empty_sheet_for_an_empty_team_is_fine(choir):
    async with db_session() as session:
        fresh = ok(await teams.create(session, None, "Fresh"))
        fresh_id = fresh.id
    report = await importer.run_team_sync(
        _csv_bytes([]), team_id=fresh_id, user_id=None
    )
    assert not report.has_errors, report.errors
    assert report.applied


async def test_sync_adding_an_archived_volunteer_reactivates_them(choir):
    """Archiving is an act in the app now, never a consequence of a sheet —
    but putting an archived person on a team still reactivates them, which is
    how a leader brings somebody back."""
    async with db_session() as session:
        gone = ok(
            await volunteers.create(session, None, "Greta", "Gone", "greta@example.org")
        )
        gone.is_active = False
        await session.flush()

    content = _csv_bytes(
        [["", "Greta", "Gone", "greta@example.org", "", "", "Choir", "member"]]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert report.applied, report.errors
    assert report.volunteers_reactivated == 1

    async with db_session() as session:
        (greta,) = await volunteers.search(session, "greta@example.org")
        assert greta.is_active, "joining a team implies active"


async def test_a_sheet_cannot_rewrite_the_contact_details_of_an_outsider(choir):
    """A team's sheet is edited by whoever holds its Drive share and applied
    overnight with nobody watching. Before this it ran unrestricted, so pasting
    a stranger's row rewrote their address on the spot — point it at your own
    mailbox, then ask for their invite, and the account is yours. The sheet may
    still add people; it may only edit the ones already on the roster."""
    async with db_session() as session:
        outsider = ok(
            await volunteers.create(
                session, None, "Orla", "Outsider", "orla@example.org"
            )
        )
        other = ok(await teams.create(session, None, "Altar Servers"))
        ok(
            await memberships.assign(
                session, None, outsider.id, other.id, TeamRole.member
            )
        )
        outsider_id = outsider.id

    poisoned = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
            ["", "Dora", "Done", "dora@example.org", "", "", "Choir", "member"],
            [outsider_id, "Orla", "Outsider", "attacker@evil.example", "", "", "", ""],
        ]
    )
    report = await importer.run_team_sync(
        poisoned, team_id=choir["choir"], user_id=None
    )
    assert not report.applied, "all-or-nothing: the whole sheet rolls back"
    assert any("not allowed to edit" in i.message for i in report.errors)

    async with db_session() as session:
        still = await volunteers.get(session, outsider_id)
        assert still.email == "orla@example.org", "her address is untouched"


async def test_a_redirected_address_is_reported_so_the_old_mailbox_can_be_told(choir):
    """Replacing an address is reported for the caller to mail; filling a blank
    one is not — there is no mailbox to tell.

    Both rows carry an ID: matching is email-first with no name fallback, so a
    blank-ID row with a new address creates a second person instead of moving
    the first (the rule test_sheets_matching pins). Mia is on the roster
    already, so the sheet may move her — that is the licence working, not a
    hole; what it may not do is move somebody it merely lists."""
    async with db_session() as session:
        blank = ok(
            await volunteers.create(session, None, "Basil", "Blank")
        )  # no address
        ok(
            await memberships.assign(
                session, None, blank.id, choir["choir"], TeamRole.member
            )
        )
        blank_id = blank.id

    moved = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            [
                choir["mia"],
                "Mia",
                "Member",
                "mia.new@example.org",
                "",
                "",
                "Choir",
                "member",
            ],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
            ["", "Dora", "Done", "dora@example.org", "", "", "Choir", "member"],
            [
                blank_id,
                "Basil",
                "Blank",
                "basil@example.org",
                "",
                "",
                "Choir",
                "member",
            ],
        ]
    )
    report = await importer.run_team_sync(moved, team_id=choir["choir"], user_id=None)
    assert report.applied, report.errors
    assert report.addresses_replaced == [("mia@example.org", "mia.new@example.org")], (
        "Mia moved and gets a notice; Basil's blank was filled in, so nobody does"
    )


# --- the production default: a sheet never removes anybody -------------------


async def test_the_default_sync_never_removes_a_missing_member(choir):
    """The contract jobs.roster_sync runs under. Dora is absent from the sheet
    and stays on the roster regardless — the write-back leg is what reconciles
    the sheet, by putting her back into it."""
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert report.applied
    assert choir["dora"] in await _team_volunteer_ids(choir["choir"])


async def test_the_default_sync_still_adds_and_updates(choir):
    """Not removing is not the same as not applying: the sheet is still the
    way a leader adds somebody and fixes a phone number."""
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            [
                "",
                "Nina",
                "New",
                "nina@example.org",
                "416-555-0143",
                "",
                "Choir",
                "member",
            ],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert report.applied
    assert report.volunteers_created == 1
    ids = await _team_volunteer_ids(choir["choir"])
    # everyone who was there is still there, and Nina joined them
    assert {choir["lena"], choir["mia"], choir["carl"], choir["dora"]} <= ids
    assert len(ids) == 5


async def test_an_empty_sheet_is_harmless_when_nothing_is_removed(choir):
    """An emptied sheet is simply a no-op, which is the better failure mode:
    the write-back leg refills it the same night."""
    report = await importer.run_team_sync(
        _csv_bytes([]), team_id=choir["choir"], user_id=None
    )
    assert report.applied and not report.has_errors
    assert await _team_volunteer_ids(choir["choir"]) == {
        choir["lena"],
        choir["mia"],
        choir["carl"],
        choir["dora"],
    }
