"""The drive_sync job over a hand-made work dir — no Drive, no rclone.

The rclone legs live in deploy/templates/volunteerdb-drive-sync.sh.j2 and are
exercised by the one-time runbook in docs/how-to/drive-roster-sync.md; these
tests cover everything the Python side decides: matching, staleness, renames,
per-team isolation, the never-overwrite-a-failed-sheet rule, and record's
bookkeeping.
"""

import csv
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from volunteerdb.db import db_session
from volunteerdb.jobs import drive_sync
from volunteerdb.models import TeamRole, TeamSheet
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.sheets import exporter
from volunteerdb.sheets.common import ROSTER_HEADERS

NOW = datetime.now(UTC)
NAME = "choir-membership-list"


def _workdir(tmp_path: Path) -> Path:
    for sub in ("in", "out", "post"):
        (tmp_path / sub).mkdir()
    return tmp_path


def _listing(workdir: Path, entries: list[tuple[str, str, datetime]], sub="in"):
    (workdir / sub / "listing.json").write_text(
        json.dumps(
            [
                # nanosecond ModTime, as rclone actually emits it
                {
                    "Name": f"{name}.csv",
                    "ID": fid,
                    "ModTime": f"{ts:%Y-%m-%dT%H:%M:%S}.123456789+00:00",
                }
                for name, fid, ts in entries
            ]
        )
    )


def _sheet_csv(rows: list[list]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ROSTER_HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


@pytest.fixture
async def choir(database):
    async with db_session() as session:
        choir = await teams.create(session, "Choir")
        lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
        mia = await volunteers.create(session, "Mia", "Member", "mia@example.org")
        await memberships.assign(session, lena.id, choir.id, TeamRole.leader)
        await memberships.assign(session, mia.id, choir.id, TeamRole.member)
        return {"choir": choir.id, "lena": lena.id, "mia": mia.id}


async def _sheet_row(team_id: int) -> TeamSheet | None:
    async with db_session() as session:
        return await session.get(TeamSheet, team_id)


async def test_bootstrap_writes_out_csv_for_sheetless_team(choir, tmp_path):
    workdir = _workdir(tmp_path)
    _listing(workdir, [])

    assert await drive_sync.apply(workdir) == 0

    out = workdir / "out" / f"{NAME}.csv"
    assert out.exists(), "a team without a sheet gets one bootstrapped"
    async with db_session() as session:
        fresh = await exporter.export_csv(
            session, team_id=choir["choir"], subtree=False
        )
    assert out.read_bytes() == fresh, "regenerated CSV is exactly a fresh export"
    sheet = await _sheet_row(choir["choir"])
    assert sheet.last_status == "new" and sheet.last_error is None
    assert (workdir / "alerts.txt").read_text() == ""
    assert (workdir / "renames.txt").read_text() == ""


async def test_sheet_edits_apply_and_regenerate_mirrors_db(choir, tmp_path):
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW)])
    # Mia promoted to core, Lena kept; a new singer joins
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [
                [
                    "Lena",
                    "Leader",
                    "lena@example.org",
                    "",
                    "",
                    "",
                    "Choir",
                    "leader",
                    "",
                    "",
                ],
                [
                    "Mia",
                    "Member",
                    "mia@example.org",
                    "",
                    "",
                    "",
                    "Choir",
                    "core",
                    "",
                    "",
                ],
                [
                    "Nora",
                    "New",
                    "nora@example.org",
                    "",
                    "",
                    "",
                    "Choir",
                    "member",
                    "",
                    "",
                ],
            ]
        )
    )

    assert await drive_sync.apply(workdir) == 0

    async with db_session() as session:
        roster = {
            v.email: m.role for m, v in await teams.roster(session, choir["choir"])
        }
    assert roster == {
        "lena@example.org": TeamRole.leader,
        "mia@example.org": TeamRole.core,
        "nora@example.org": TeamRole.member,
    }
    sheet = await _sheet_row(choir["choir"])
    assert sheet.last_status == "applied"
    out = workdir / "out" / f"{NAME}.csv"
    assert out.exists(), "the DB state is mirrored back after an apply"
    body = out.read_bytes().decode("utf-8-sig")
    assert "nora@example.org" in body and "Core team member" in body


async def test_stale_modtime_skips_apply_but_mirrors_db_changes(choir, tmp_path):
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW - timedelta(days=2))])
    # the sheet still shows Mia; the DB has since removed her via the UI
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [
                [
                    "Lena",
                    "Leader",
                    "lena@example.org",
                    "",
                    "",
                    "",
                    "Choir",
                    "leader",
                    "",
                    "",
                ],
                [
                    "Mia",
                    "Member",
                    "mia@example.org",
                    "",
                    "",
                    "",
                    "Choir",
                    "member",
                    "",
                    "",
                ],
            ]
        )
    )
    async with db_session() as session:
        session.add(
            TeamSheet(team_id=choir["choir"], file_id="f123", last_synced_at=NOW)
        )
        membership = await memberships.find(session, choir["mia"], choir["choir"])
        await memberships.remove(session, membership.id)

    assert await drive_sync.apply(workdir) == 0

    async with db_session() as session:
        roster = [v.email for _, v in await teams.roster(session, choir["choir"])]
    assert roster == ["lena@example.org"], (
        "a sheet not edited since our last upload must never resurrect rows — "
        "the database wins"
    )
    sheet = await _sheet_row(choir["choir"])
    assert sheet.last_status == "unchanged"
    body = (workdir / "out" / f"{NAME}.csv").read_bytes().decode("utf-8-sig")
    assert "mia@example.org" not in body, "the upload will push the DB state out"


async def test_identical_sheet_skips_the_upload(choir, tmp_path):
    workdir = _workdir(tmp_path)
    async with db_session() as session:
        current = await exporter.export_csv(
            session, team_id=choir["choir"], subtree=False
        )
    _listing(workdir, [(NAME, "f123", NOW - timedelta(days=2))])
    (workdir / "in" / f"{NAME}.csv").write_bytes(current)
    async with db_session() as session:
        session.add(
            TeamSheet(team_id=choir["choir"], file_id="f123", last_synced_at=NOW)
        )

    assert await drive_sync.apply(workdir) == 0
    assert not (workdir / "out" / f"{NAME}.csv").exists(), (
        "nothing changed on either side — no upload, no Drive version churn"
    )


async def test_failed_sheet_is_never_overwritten(choir, tmp_path):
    """A leader's broken edit stays on Drive to be fixed; and other teams
    still sync (per-team isolation)."""
    async with db_session() as session:
        ushers = await teams.create(session, "Ushers")
        otto = await volunteers.create(session, "Otto", "Usher", "otto@example.org")
        await memberships.assign(session, otto.id, ushers.id, TeamRole.leader)

    workdir = _workdir(tmp_path)
    _listing(
        workdir,
        [(NAME, "f123", NOW), ("ushers-membership-list", "f456", NOW)],
    )
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [
                [
                    "Lena",
                    "Leader",
                    "lena@example.org",
                    "",
                    "",
                    "oui",
                    "Choir",
                    "leader",
                    "",
                    "",
                ],
            ]
        )
    )
    (workdir / "in" / "ushers-membership-list.csv").write_bytes(
        _sheet_csv(
            [
                [
                    "Otto",
                    "Usher",
                    "otto@example.org",
                    "",
                    "",
                    "",
                    "Ushers",
                    "leader",
                    "",
                    "",
                ],
                [
                    "Uma",
                    "Usher",
                    "uma@example.org",
                    "",
                    "",
                    "",
                    "Ushers",
                    "member",
                    "",
                    "",
                ],
            ]
        )
    )

    assert await drive_sync.apply(workdir) == 0

    assert not (workdir / "out" / f"{NAME}.csv").exists(), (
        "the failed sheet must NOT be regenerated — that would erase the "
        "leader's edits instead of letting them fix the error"
    )
    sheet = await _sheet_row(choir["choir"])
    assert sheet.last_status == "error" and "'oui'" in sheet.last_error
    alerts = (workdir / "alerts.txt").read_text()
    assert "Choir" in alerts and "oui" in alerts

    async with db_session() as session:
        roster = [v.email for _, v in await teams.roster(session, choir["choir"])]
        assert sorted(roster) == ["lena@example.org", "mia@example.org"], (
            "the failed team's roster is untouched"
        )
        (uma,) = await volunteers.search(session, "uma@example.org")
        assert uma is not None, "the healthy team still applied"
    assert (workdir / "out" / "ushers-membership-list.csv").exists()


async def test_rename_manifest_when_team_slug_changed(choir, tmp_path):
    """The sheet is matched by its stable file id; a slug change shows up as a
    rename instruction for the host script, not a new file."""
    async with db_session() as session:
        session.add(TeamSheet(team_id=choir["choir"], file_id="f123"))
        await teams.update(session, choir["choir"], name="Chancel Choir")

    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW)])  # Drive still has the old name
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [
                [
                    "Lena",
                    "Leader",
                    "lena@example.org",
                    "",
                    "",
                    "",
                    "Chancel Choir",
                    "leader",
                    "",
                    "",
                ],
                [
                    "Mia",
                    "Member",
                    "mia@example.org",
                    "",
                    "",
                    "",
                    "Chancel Choir",
                    "member",
                    "",
                    "",
                ],
            ]
        )
    )

    assert await drive_sync.apply(workdir) == 0

    assert (workdir / "renames.txt").read_text() == (
        f"{NAME}\tchancel-choir-membership-list\n"
    )
    assert (workdir / "out" / "chancel-choir-membership-list.csv").exists(), (
        "the regenerated file carries the new name"
    )
    sheet = await _sheet_row(choir["choir"])
    assert sheet.last_status == "applied"


async def test_record_stores_ids_but_not_marks_for_failed_teams(choir, tmp_path):
    async with db_session() as session:
        session.add(
            TeamSheet(team_id=choir["choir"], last_status="error", last_error="x")
        )
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f-new", NOW)], sub="post")

    assert await drive_sync.record(workdir) == 0

    sheet = await _sheet_row(choir["choir"])
    assert sheet.file_id == "f-new" and sheet.file_name == NAME
    assert sheet.last_synced_at is None, (
        "a failed team's sync mark must not advance — the sheet must still "
        "count as 'newer' so the next night retries the apply"
    )

    # after a clean apply the mark advances
    async with db_session() as session:
        (await session.get(TeamSheet, choir["choir"])).last_status = "applied"
    assert await drive_sync.record(workdir) == 0
    sheet = await _sheet_row(choir["choir"])
    assert sheet.last_synced_at is not None


async def test_sync_user_is_idempotent_and_cannot_log_in(database):
    first = await drive_sync._ensure_sync_user()
    second = await drive_sync._ensure_sync_user()
    assert first == second

    async with db_session() as session:
        user = await users.get_by_email(session, drive_sync.SYNC_USER_EMAIL)
        assert user.is_active is False
        assert user.invite_token is None, "no redeemable invite"
        assert user.is_admin is False


async def test_missing_listing_is_a_systemic_failure(database, tmp_path):
    workdir = _workdir(tmp_path)
    assert await drive_sync.apply(workdir) == 1
    assert await drive_sync.record(workdir) == 1
