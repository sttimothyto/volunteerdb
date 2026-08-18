"""Sync mode: a team's sheet is that team's COMPLETE roster.

Unlike manual imports (add/update only), run_team_sync removes memberships
absent from the sheet — the history twins retain them — and archives a
volunteer who thereby loses their last membership anywhere. A safety
threshold refuses removals that would drop most of a team at once.
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
        choir = await teams.create(session, None, "Choir")
        hospitality = await teams.create(session, None, "Hospitality")
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        mia = await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
        carl = await volunteers.create(
            session, None, "Carl", "Cross", "carl@example.org"
        )
        dora = await volunteers.create(
            session, None, "Dora", "Done", "dora@example.org"
        )
        await memberships.assign(session, None, lena.id, choir.id, TeamRole.leader)
        await memberships.assign(session, None, mia.id, choir.id, TeamRole.member)
        await memberships.assign(session, None, carl.id, choir.id, TeamRole.member)
        await memberships.assign(
            session, None, carl.id, hospitality.id, TeamRole.member
        )
        await memberships.assign(session, None, dora.id, choir.id, TeamRole.member)
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
        pairs = await teams.roster(session, None, team_id)
        return {volunteer.id for _, volunteer in pairs}


async def test_sync_roundtrip_of_team_export_is_a_noop(choir):
    async with db_session() as session:
        content = await exporter.export_csv(
            session, None, team_id=choir["choir"], subtree=False
        )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.memberships_removed == 0 and report.volunteers_archived == 0
    assert report.volunteers_created == report.volunteers_updated == 0
    assert report.memberships_created == report.memberships_updated == 0


async def test_sync_applies_adds_updates_and_removals(choir):
    # Lena unchanged, Mia promoted, Nora added; Carl and Dora omitted
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
    assert report.memberships_removed == 2, "Carl and Dora left the sheet"
    assert report.volunteers_archived == 1, "only Dora lost her last membership"

    remaining = await _team_volunteer_ids(choir["choir"])
    assert choir["carl"] not in remaining and choir["dora"] not in remaining

    async with db_session() as session:
        (carl,) = await volunteers.search(session, "carl@example.org")
        assert carl.is_active, "Carl still serves on Hospitality"
        (dora,) = await volunteers.search(
            session, "dora@example.org", include_inactive=True
        )
        assert not dora.is_active, "Dora's last membership is gone — archived"
        # the stint survives in the history twin for as-of views and timelines
        archived = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(membership_history)
                .where(
                    membership_history.c.volunteer_id == choir["dora"],
                    membership_history.c.team_id == choir["choir"],
                    membership_history.c.op == "D",
                )
            )
        ).scalar_one()
        assert archived == 1


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
    assert report.applied and report.memberships_removed == 0


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


async def test_sync_safety_threshold_refuses_mass_removal(choir):
    # 1 of 4 kept → 3 removals, over half the roster and >= the minimum
    content = _csv_bytes(
        [["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"]]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert report.has_errors and not report.applied
    assert any("safety threshold" in e.message for e in report.errors)
    assert len(await _team_volunteer_ids(choir["choir"])) == 4, "roster untouched"


async def test_sync_small_removals_pass_the_threshold(choir):
    # 2 of 4 removed: over half is false (2*2 == 4), so this applies
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied and report.memberships_removed == 2


async def test_sync_row_errors_prevent_all_removals(choir):
    # Mia's Role cell is garbage; Dora is missing. Nothing may change: an
    # unparsable sheet must never read as "everyone else left".
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "oui"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert report.has_errors and not report.applied
    assert report.memberships_removed == 0
    assert choir["dora"] in await _team_volunteer_ids(choir["choir"])


async def test_sync_dry_run_reports_without_writing(choir):
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(
        content, team_id=choir["choir"], user_id=None, dry_run=True
    )
    assert not report.has_errors and not report.applied
    assert report.memberships_removed == 1
    assert choir["dora"] in await _team_volunteer_ids(choir["choir"])


async def test_sync_empty_sheet_never_wipes_a_team(choir):
    """A header-only download (or an accidentally emptied sheet) must not read
    as 'everyone left' — even for teams too small for the percentage breaker."""
    report = await importer.run_team_sync(
        _csv_bytes([]), team_id=choir["choir"], user_id=None
    )
    assert report.has_errors and not report.applied
    assert any("refusing to empty the team" in e.message for e in report.errors)
    assert len(await _team_volunteer_ids(choir["choir"])) == 4, "roster untouched"

    # a 2-member team slips under SYNC_REMOVAL_MIN — the empty-sheet guard
    # still refuses the wipe
    async with db_session() as session:
        duo = await teams.create(session, None, "Duo")
        for volunteer_id in (choir["lena"], choir["mia"]):
            await memberships.assign(
                session, None, volunteer_id, duo.id, TeamRole.member
            )
        duo_id = duo.id
    report = await importer.run_team_sync(_csv_bytes([]), team_id=duo_id, user_id=None)
    assert report.has_errors and not report.applied
    assert len(await _team_volunteer_ids(duo_id)) == 2, "roster untouched"


async def test_sync_empty_sheet_for_an_empty_team_is_fine(choir):
    async with db_session() as session:
        fresh = await teams.create(session, None, "Fresh")
        fresh_id = fresh.id
    report = await importer.run_team_sync(
        _csv_bytes([]), team_id=fresh_id, user_id=None
    )
    assert not report.has_errors, report.errors
    assert report.applied and report.memberships_removed == 0


async def test_sync_create_plus_remove_raises_a_churn_warning(choir):
    """An edited email cell on a blank-ID row is invisible as such: it looks
    like one person left and a new one joined. The sync applies, but warns."""
    content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia.member@example.org", "", "", "Choir", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
            ["", "Dora", "Done", "dora@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(content, team_id=choir["choir"], user_id=None)
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.volunteers_created == 1, "the new address created a second Mia"
    assert report.memberships_removed == 1, "the old Mia fell off the sheet"
    assert report.churn_suspected
    assert any("may now exist twice" in w.message for w in report.warnings)


async def test_sync_readding_an_archived_volunteer_reactivates(choir):
    # remove Dora (her only membership) → archived, then re-add her by row
    keep_three = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(
        keep_three, team_id=choir["choir"], user_id=None
    )
    assert report.applied and report.volunteers_archived == 1

    with_dora = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
            ["", "Carl", "Cross", "carl@example.org", "", "", "Choir", "member"],
            ["", "Dora", "Done", "dora@example.org", "", "", "Choir", "member"],
        ]
    )
    report = await importer.run_team_sync(
        with_dora, team_id=choir["choir"], user_id=None
    )
    assert report.applied, report.errors
    assert report.volunteers_reactivated == 1

    async with db_session() as session:
        (dora,) = await volunteers.search(session, "dora@example.org")
        assert dora.is_active, "joining a team implies active"


async def test_a_sheet_cannot_rewrite_the_contact_details_of_an_outsider(choir):
    """A team's sheet is edited by whoever holds its Drive share and applied
    overnight with nobody watching. Before this it ran unrestricted, so pasting
    a stranger's row rewrote their address on the spot — point it at your own
    mailbox, then ask for their invite, and the account is yours. The sheet may
    still add people; it may only edit the ones already on the roster."""
    async with db_session() as session:
        outsider = await volunteers.create(
            session, None, "Orla", "Outsider", "orla@example.org"
        )
        other = await teams.create(session, None, "Altar Servers")
        await memberships.assign(session, None, outsider.id, other.id, TeamRole.member)
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
        blank = await volunteers.create(session, None, "Basil", "Blank")  # no address
        await memberships.assign(
            session, None, blank.id, choir["choir"], TeamRole.member
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
