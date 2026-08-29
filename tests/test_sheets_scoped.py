"""Leader/second imports are scoped to managed teams, row by row."""

import csv
from io import StringIO

import pytest

from volunteerdb import errors
from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.sheets import exporter, importer
from volunteerdb.sheets.common import ROSTER_HEADERS

from tests import mint
from tests.fp_helpers import ok, refused


@pytest.fixture
async def parish(database):
    """Liturgy > Music; separate Hospitality. Lena leads Liturgy; Mia is a
    Liturgy member; Otto belongs to Hospitality only."""
    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        music = ok(
            await teams.create(session, None, "Music", parent_team_id=liturgy.id)
        )
        hospitality = ok(await teams.create(session, None, "Hospitality"))
        lena = ok(
            await volunteers.create(session, None, "Lena", "Leader", "lena@example.org")
        )
        mia = ok(
            await volunteers.create(
                session, None, "Mia", "Member", "mia@example.org", phone="555-1"
            )
        )
        otto = ok(
            await volunteers.create(
                session, None, "Otto", "Out", "otto@example.org", phone="555-2"
            )
        )
        ok(
            await memberships.assign(
                session, None, lena.id, liturgy.id, TeamRole.leader
            )
        )
        ok(await memberships.assign(session, None, mia.id, liturgy.id, TeamRole.member))
        ok(
            await memberships.assign(
                session, None, otto.id, hospitality.id, TeamRole.member
            )
        )
        leader, _ = ok(
            await users.create(
                session,
                "lena@example.org",
                volunteer_id=lena.id,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        member, _ = ok(
            await users.create(
                session,
                "mia@example.org",
                volunteer_id=mia.id,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        return {
            "liturgy": liturgy.id,
            "music": music.id,
            "hospitality": hospitality.id,
            "leader_uid": leader.id,
            "member_uid": member.id,
            "mia_vid": mia.id,
            "otto_vid": otto.id,
        }


def _csv_bytes(rows: list[list]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ROSTER_HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


async def test_membership_on_managed_subtree_applied(parish, env):
    content = _csv_bytes(
        [["", "Mia", "Member", "mia@example.org", "", "", "Liturgy / Music", "member"]]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert not report.has_errors, report.errors
    assert report.applied and report.memberships_created == 1


async def test_unmanaged_team_row_blocks_everything(parish, env):
    content = _csv_bytes(
        [
            [
                "",
                "Mia",
                "Member",
                "mia@example.org",
                "",
                "",
                "Liturgy / Music",
                "member",
            ],
            ["", "Otto", "Out", "otto@example.org", "", "", "Hospitality", "core"],
        ]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert report.has_errors and not report.applied
    # the volunteer columns are checked first, so the row is refused as an
    # unauthorised contact edit before the team check is ever reached
    assert any("not allowed to edit volunteer" in e.message for e in report.errors)
    async with db_session() as session:
        rows = await volunteers.assignments(session, parish["mia_vid"])
        assert len(rows) == 1, "all-or-nothing: the valid row rolled back too"


async def test_new_volunteer_with_managed_membership_created(parish, env):
    content = _csv_bytes(
        [["", "Nora", "New", "nora@example.org", "", "", "Liturgy", "member"]]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert not report.has_errors, report.errors
    assert (
        report.applied
        and report.volunteers_created == 1
        and report.memberships_created == 1
    )


async def test_new_volunteer_without_membership_rejected(parish, env):
    content = _csv_bytes([["", "Nora", "New", "nora@example.org", "", "", "", ""]])
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert report.has_errors and not report.applied
    assert any("must be put on a team you lead" in e.message for e in report.errors)


async def test_new_volunteer_with_unmanaged_membership_rejected(parish, env):
    content = _csv_bytes(
        [["", "Nora", "New", "nora@example.org", "", "", "Hospitality", "member"]]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert not report.applied
    assert any("must be put on a team you lead" in e.message for e in report.errors)


async def test_contact_update_of_managed_volunteer_applied(parish, env):
    content = _csv_bytes(
        [["", "Mia", "Member", "mia@example.org", "555-99", "", "", ""]]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert not report.has_errors, report.errors
    assert report.applied and report.volunteers_updated == 1
    async with db_session() as session:
        (mia,) = await volunteers.search(session, "Mia")
        assert mia.phone == "555-99"


async def test_update_of_outsider_rejected_even_when_identical(parish, env):
    # values match Otto's current record exactly — still denied, otherwise a
    # dry-run would confirm guessed contact details
    content = _csv_bytes([["", "Otto", "Out", "otto@example.org", "555-2", "", "", ""]])
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert report.has_errors and not report.applied
    assert any("not allowed to edit volunteer" in e.message for e in report.errors)


async def test_update_of_outsider_by_id_rejected(parish, env):
    """An ID pins the row to Otto just as surely as his email does — the scope
    check must treat both the same."""
    content = _csv_bytes(
        [
            [
                str(parish["otto_vid"]),
                "Otto",
                "Out",
                "otto@example.org",
                "555-2",
                "",
                "",
                "",
            ]
        ]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert report.has_errors and not report.applied
    assert any("not allowed to edit volunteer" in e.message for e in report.errors)


async def test_id_row_on_managed_team_grants_the_contact_edit(parish, env):
    """A leader's own export carries IDs on every row — the scoping pre-pass
    must resolve them, or scoped exports stop round-tripping."""
    content = _csv_bytes(
        [
            [
                str(parish["otto_vid"]),
                "Otto",
                "Out",
                "otto@example.org",
                "555-77",
                "",
                "Liturgy",
                "member",
            ]
        ]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert not report.has_errors, report.errors
    assert (
        report.applied
        and report.volunteers_updated == 1
        and report.memberships_created == 1
    )


async def test_update_ok_when_same_row_adds_them_to_managed_team(parish, env):
    content = _csv_bytes(
        [["", "Otto", "Out", "otto@example.org", "555-77", "", "Liturgy", "member"]]
    )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert not report.has_errors, report.errors
    assert (
        report.applied
        and report.volunteers_updated == 1
        and report.memberships_created == 1
    )
    async with db_session() as session:
        (otto,) = await volunteers.search(session, "Otto")
        assert otto.phone == "555-77"


async def test_scoped_export_reimports_as_noop(parish, env):
    async with db_session() as session:
        content = ok(
            await exporter.export_csv(session, None, team_ids={parish["liturgy"]})
        )
    report = ok(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["leader_uid"]
        )
    )
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.volunteers_created == report.volunteers_updated == 0
    assert report.memberships_created == report.memberships_updated == 0


async def test_a_user_who_leads_nothing_cannot_import_at_all(parish, env):
    """The right to import lives in run_import now, not at the two front doors,
    so a plain member is refused before a single row is read — one message
    instead of one per row, and the same answer on both surfaces.

    Row-by-row scoping is still what bounds a caller who *does* hold the right:
    every other test in this file exercises it."""
    content = _csv_bytes(
        [
            ["", "Nora", "New", "nora@example.org", "", "", "", ""],
            ["", "Mia", "Member", "mia@example.org", "", "", "Liturgy", "core"],
        ]
    )
    refused(
        await importer.run_import(
            env, content, dry_run=False, user_id=parish["member_uid"]
        ),
        errors.Forbidden,
        match="import spreadsheets",
    )
    async with db_session() as session:
        assert await volunteers.find_by_email(session, "nora@example.org") == []
