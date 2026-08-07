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
        choir = await teams.create(session, "Choir")
        hospitality = await teams.create(session, "Hospitality")
        lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
        mia = await volunteers.create(session, "Mia", "Member", "mia@example.org")
        carl = await volunteers.create(session, "Carl", "Cross", "carl@example.org")
        dora = await volunteers.create(session, "Dora", "Done", "dora@example.org")
        await memberships.assign(session, lena.id, choir.id, TeamRole.leader)
        await memberships.assign(session, mia.id, choir.id, TeamRole.member)
        await memberships.assign(session, carl.id, choir.id, TeamRole.member)
        await memberships.assign(session, carl.id, hospitality.id, TeamRole.member)
        await memberships.assign(session, dora.id, choir.id, TeamRole.member)
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
        pairs = await teams.roster(session, team_id)
        return {volunteer.id for _, volunteer in pairs}


async def test_sync_roundtrip_of_team_export_is_a_noop(choir):
    async with db_session() as session:
        content = await exporter.export_csv(
            session, team_id=choir["choir"], subtree=False
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
        duo = await teams.create(session, "Duo")
        for volunteer_id in (choir["lena"], choir["mia"]):
            await memberships.assign(session, volunteer_id, duo.id, TeamRole.member)
        duo_id = duo.id
    report = await importer.run_team_sync(_csv_bytes([]), team_id=duo_id, user_id=None)
    assert report.has_errors and not report.applied
    assert len(await _team_volunteer_ids(duo_id)) == 2, "roster untouched"


async def test_sync_empty_sheet_for_an_empty_team_is_fine(choir):
    async with db_session() as session:
        fresh = await teams.create(session, "Fresh")
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
