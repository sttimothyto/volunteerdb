"""Leader/second imports are scoped to managed teams, row by row."""

from io import BytesIO

import pytest
from openpyxl import load_workbook

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.sheets import exporter, importer
from volunteerdb.sheets.common import MEMBERSHIP_SHEET, VOLUNTEER_SHEET


@pytest.fixture
async def parish(database):
    """Liturgy > Music; separate Hospitality. Lena leads Liturgy; Mia is a
    Liturgy member; Otto belongs to Hospitality only."""
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        music = await teams.create(session, "Music", parent_team_id=liturgy.id)
        hospitality = await teams.create(session, "Hospitality")
        lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
        mia = await volunteers.create(
            session, "Mia", "Member", "mia@example.org", phone="555-1"
        )
        otto = await volunteers.create(
            session, "Otto", "Out", "otto@example.org", phone="555-2"
        )
        await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
        await memberships.assign(session, mia.id, liturgy.id, TeamRole.member)
        await memberships.assign(session, otto.id, hospitality.id, TeamRole.member)
        leader = await users.create(
            session, "lena@example.org", volunteer_id=lena.id, password="pw"
        )
        member = await users.create(
            session, "mia@example.org", volunteer_id=mia.id, password="pw"
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


def _workbook_bytes(vol_rows=(), mem_rows=()) -> bytes:
    wb = load_workbook(BytesIO(exporter.template_workbook()))
    for row in vol_rows:
        wb[VOLUNTEER_SHEET].append(row)
    for row in mem_rows:
        wb[MEMBERSHIP_SHEET].append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def test_membership_on_managed_subtree_applied(parish):
    content = _workbook_bytes(
        mem_rows=[
            ["mia@example.org", "Mia Member", "Liturgy / Music", "member", None, None]
        ]
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert not report.has_errors, report.errors
    assert report.applied and report.memberships_created == 1


async def test_unmanaged_team_row_blocks_everything(parish):
    content = _workbook_bytes(
        mem_rows=[
            ["mia@example.org", "Mia Member", "Liturgy / Music", "member", None, None],
            ["otto@example.org", "Otto Out", "Hospitality", "core", None, None],
        ]
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert report.has_errors and not report.applied
    assert any("not a team you lead" in e.message for e in report.errors)
    async with db_session() as session:
        rows = await volunteers.assignments(session, parish["mia_vid"])
        assert len(rows) == 1, "all-or-nothing: the valid row rolled back too"


async def test_new_volunteer_with_managed_membership_created(parish):
    content = _workbook_bytes(
        vol_rows=[["Nora", "New", "nora@example.org", None, None, "yes"]],
        mem_rows=[["nora@example.org", "Nora New", "Liturgy", "member", None, None]],
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert not report.has_errors, report.errors
    assert (
        report.applied
        and report.volunteers_created == 1
        and report.memberships_created == 1
    )


async def test_new_volunteer_without_membership_rejected(parish):
    content = _workbook_bytes(
        vol_rows=[["Nora", "New", "nora@example.org", None, None, "yes"]]
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert report.has_errors and not report.applied
    assert any(
        "must also be added to a team you lead" in e.message for e in report.errors
    )


async def test_new_volunteer_with_unmanaged_membership_rejected(parish):
    content = _workbook_bytes(
        vol_rows=[["Nora", "New", "nora@example.org", None, None, "yes"]],
        mem_rows=[
            ["nora@example.org", "Nora New", "Hospitality", "member", None, None]
        ],
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert not report.applied
    assert any(
        "must also be added to a team you lead" in e.message for e in report.errors
    )
    assert any("not a team you lead" in e.message for e in report.errors)


async def test_contact_update_of_managed_volunteer_applied(parish):
    content = _workbook_bytes(
        vol_rows=[["Mia", "Member", "mia@example.org", "555-99", None, "yes"]]
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert not report.has_errors, report.errors
    assert report.applied and report.volunteers_updated == 1
    async with db_session() as session:
        (mia,) = await volunteers.search(session, "Mia")
        assert mia.phone == "555-99"


async def test_update_of_outsider_rejected_even_when_identical(parish):
    # values match Otto's current record exactly — still denied, otherwise a
    # dry-run would confirm guessed contact details
    content = _workbook_bytes(
        vol_rows=[["Otto", "Out", "otto@example.org", "555-2", None, "yes"]]
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert report.has_errors and not report.applied
    assert any("not allowed to edit volunteer" in e.message for e in report.errors)


async def test_update_ok_when_same_file_adds_them_to_managed_team(parish):
    content = _workbook_bytes(
        vol_rows=[["Otto", "Out", "otto@example.org", "555-77", None, "yes"]],
        mem_rows=[["otto@example.org", "Otto Out", "Liturgy", "member", None, None]],
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
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


async def test_scoped_export_reimports_as_noop(parish):
    async with db_session() as session:
        content = await exporter.export_workbook(session, team_ids={parish["liturgy"]})
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["leader_uid"]
    )
    assert not report.has_errors, report.errors
    assert report.applied
    assert report.volunteers_created == report.volunteers_updated == 0
    assert report.memberships_created == report.memberships_updated == 0


async def test_user_without_managed_teams_gets_row_errors(parish):
    # the API gate 403s such users; the importer still refuses row-by-row
    content = _workbook_bytes(
        vol_rows=[["Nora", "New", "nora@example.org", None, None, "yes"]],
        mem_rows=[["mia@example.org", "Mia Member", "Liturgy", "core", None, None]],
    )
    report = await importer.run_import(
        content, dry_run=False, user_id=parish["member_uid"]
    )
    assert report.has_errors and not report.applied
    assert len(report.errors) == 2
