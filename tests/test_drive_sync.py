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
        choir = await teams.create(session, None, "Choir")
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        mia = await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
        await memberships.assign(session, None, lena.id, choir.id, TeamRole.leader)
        await memberships.assign(session, None, mia.id, choir.id, TeamRole.member)
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
            session, None, team_id=choir["choir"], subtree=False
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
                ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
                ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "core"],
                ["", "Nora", "New", "nora@example.org", "", "", "Choir", "member"],
            ]
        )
    )

    assert await drive_sync.apply(workdir) == 0

    async with db_session() as session:
        roster = {
            v.email: m.role
            for m, v in await teams.roster(session, None, choir["choir"])
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
                ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
                ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
            ]
        )
    )
    async with db_session() as session:
        session.add(
            TeamSheet(team_id=choir["choir"], file_id="f123", last_synced_at=NOW)
        )
        membership = await memberships.find(session, choir["mia"], choir["choir"])
        await memberships.remove(session, None, membership.id)

    assert await drive_sync.apply(workdir) == 0

    async with db_session() as session:
        roster = [v.email for _, v in await teams.roster(session, None, choir["choir"])]
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
            session, None, team_id=choir["choir"], subtree=False
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
        ushers = await teams.create(session, None, "Ushers")
        otto = await volunteers.create(
            session, None, "Otto", "Usher", "otto@example.org"
        )
        await memberships.assign(session, None, otto.id, ushers.id, TeamRole.leader)

    workdir = _workdir(tmp_path)
    _listing(
        workdir,
        [(NAME, "f123", NOW), ("ushers-membership-list", "f456", NOW)],
    )
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [
                ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "oui"],
            ]
        )
    )
    (workdir / "in" / "ushers-membership-list.csv").write_bytes(
        _sheet_csv(
            [
                ["", "Otto", "Usher", "otto@example.org", "", "", "Ushers", "leader"],
                ["", "Uma", "Usher", "uma@example.org", "", "", "Ushers", "member"],
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
        roster = [v.email for _, v in await teams.roster(session, None, choir["choir"])]
        assert sorted(roster) == ["lena@example.org", "mia@example.org"], (
            "the failed team's roster is untouched"
        )
        (uma,) = await volunteers.search(session, "uma@example.org")
        assert uma is not None, "the healthy team still applied"
    assert (workdir / "out" / "ushers-membership-list.csv").exists()


async def test_manifest_carries_team_paths_and_editors_to_the_host(choir, tmp_path):
    """The host's decorate/share leg cannot reach the database, so apply hands
    it the Team dropdown values and who may edit each sheet."""
    async with db_session() as session:
        sopranos = await teams.create(
            session, None, "Sopranos", parent_team_id=choir["choir"]
        )
        sam = await volunteers.create(session, None, "Sam", "Second", "sam@example.org")
        await memberships.assign(session, None, sam.id, sopranos.id, TeamRole.second)
        await teams.create(session, None, "Flowers")  # nobody in charge

    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW)])
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv([["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "oui"]])
    )

    assert await drive_sync.apply(workdir) == 0
    manifest = json.loads((workdir / "manifest.json").read_text())

    assert manifest["teams"] == ["Choir", "Choir / Sopranos", "Flowers"], (
        "the template's dropdown offers every active team, as a display path"
    )
    assert manifest["sheets"]["choir-sopranos-membership-list"] == {
        "team": "Choir / Sopranos",
        "editors": ["sam@example.org"],  # a second counts, a plain member does not
    }
    assert manifest["sheets"]["flowers-membership-list"]["editors"] == []
    assert manifest["sheets"][NAME]["editors"] == ["lena@example.org"], (
        "a sheet that failed to apply still gets shared — its leader is exactly "
        "the person who has to go in and fix it"
    )
    assert (await _sheet_row(choir["choir"])).last_status == "error"


async def test_applied_sheet_with_suspected_churn_still_alerts(choir, tmp_path):
    """An edited email on a blank-ID row applies as remove-plus-create; the
    sheet is fine, but the alert email must still fire so a human checks for
    the duplicate."""
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW)])
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [
                ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
                ["", "Mia", "Member", "mia.new@example.org", "", "", "Choir", "member"],
            ]
        )
    )

    assert await drive_sync.apply(workdir) == 0

    sheet = await _sheet_row(choir["choir"])
    assert sheet.last_status == "applied" and sheet.last_error is None
    alerts = (workdir / "alerts.txt").read_text()
    assert "WARNING" in alerts and "duplicated person" in alerts
    assert (workdir / "out" / f"{NAME}.csv").exists(), (
        "an applied sheet is still regenerated — the warning does not block"
    )


async def test_rename_manifest_when_team_slug_changed(choir, tmp_path):
    """The sheet is matched by its stable file id; a slug change shows up as a
    rename instruction for the host script, not a new file."""
    async with db_session() as session:
        session.add(TeamSheet(team_id=choir["choir"], file_id="f123"))
        await teams.update(session, None, choir["choir"], name="Chancel Choir")

    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW)])  # Drive still has the old name
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [
                [
                    "",
                    "Lena",
                    "Leader",
                    "lena@example.org",
                    "",
                    "",
                    "Chancel Choir",
                    "leader",
                ],
                [
                    "",
                    "Mia",
                    "Member",
                    "mia@example.org",
                    "",
                    "",
                    "Chancel Choir",
                    "member",
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
        user = await users.get_by_email(session, drive_sync.sync_user_email())
        assert user.is_active is False
        assert user.invite_token is None, "no redeemable invite"
        assert user.is_admin is False


def test_sync_user_follows_the_sender_domain(monkeypatch):
    """Derived from VDB_MAIL_FROM rather than configured separately, so it
    cannot land on a domain the parish does not own.

    The St. Timothy value is asserted explicitly because that instance already
    has a drive-sync@sttimothyto.org account with history attributed to it —
    deriving a different address would orphan those rows behind a second user.
    """
    from volunteerdb.config import settings

    monkeypatch.setenv("VDB_MAIL_FROM", "no-reply@sttimothyto.org")
    settings.cache_clear()
    assert drive_sync.sync_user_email() == "drive-sync@sttimothyto.org"

    monkeypatch.setenv("VDB_MAIL_FROM", "no-reply@stpeters.example.org")
    settings.cache_clear()
    assert drive_sync.sync_user_email() == "drive-sync@stpeters.example.org"
    settings.cache_clear()


async def test_missing_listing_is_a_systemic_failure(database, tmp_path):
    workdir = _workdir(tmp_path)
    assert await drive_sync.apply(workdir) == 1
    assert await drive_sync.record(workdir) == 1


async def _request(team_id: int, file_id: str, *, import_rows: bool = False) -> None:
    """What an admin's "Change roster spreadsheet" does, minus the URL parsing."""
    async with db_session() as session:
        await teams.request_roster_sheet(
            session,
            None,
            team_id,
            f"https://docs.google.com/spreadsheets/d/{file_id}",
            import_rows=import_rows,
        )


async def test_repoint_overwrites_the_new_sheet_from_the_database(choir, tmp_path):
    """The default direction. The linked sheet's own rows are NOT imported —
    they are replaced by the database's, which is the answer that cannot lose
    parish data."""
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW), ("choir-copy", "f999", NOW)])
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"]]
        )
    )
    # a stale copy a leader had been keeping: its rows must not reach the DB
    (workdir / "in" / "choir-copy.csv").write_bytes(
        _sheet_csv([["", "Zoe", "Stale", "zoe@example.org", "", "", "Choir", "member"]])
    )
    await _request(choir["choir"], "f999")

    assert await drive_sync.apply(workdir) == 0

    sheet = await _sheet_row(choir["choir"])
    assert sheet.file_id == "f999" and sheet.file_name == "choir-copy"
    assert sheet.requested_file_id is None, "the request is spent, not standing"
    assert sheet.last_status == "unchanged", "the DB won; nothing was imported"

    async with db_session() as session:
        roster = [v.email for _, v in await teams.roster(session, None, choir["choir"])]
    assert sorted(roster) == ["lena@example.org", "mia@example.org"], (
        "the linked sheet's rows never touched the roster"
    )

    renames = (workdir / "renames.txt").read_text().splitlines()
    assert renames == [f"{NAME}\t{NAME}-replaced-f123", f"choir-copy\t{NAME}"], (
        "the outgoing sheet is parked BEFORE the incoming one takes its name — "
        "Drive would otherwise hold two files called the same thing"
    )
    out = (workdir / "out" / f"{NAME}.csv").read_bytes().decode("utf-8-sig")
    assert "mia@example.org" in out and "zoe" not in out.lower()


async def test_repoint_can_import_the_new_sheet_instead(choir, tmp_path):
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW), ("choir-copy", "f999", NOW)])
    (workdir / "in" / f"{NAME}.csv").write_bytes(_sheet_csv([]))
    (workdir / "in" / "choir-copy.csv").write_bytes(
        _sheet_csv(
            [
                ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
                ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
                ["", "Nora", "New", "nora@example.org", "", "", "Choir", "member"],
            ]
        )
    )
    await _request(choir["choir"], "f999", import_rows=True)

    assert await drive_sync.apply(workdir) == 0

    sheet = await _sheet_row(choir["choir"])
    assert sheet.file_id == "f999" and sheet.last_status == "applied"
    async with db_session() as session:
        roster = [v.email for _, v in await teams.roster(session, None, choir["choir"])]
    assert "nora@example.org" in roster, "the linked sheet's rows were imported"


async def test_repoint_to_an_invisible_sheet_keeps_the_current_one(choir, tmp_path):
    """The drive.file grant sees only files this system created, so a sheet made
    by hand anywhere else is simply absent from the listing."""
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW)])
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"]]
        )
    )
    async with db_session() as session:
        session.add(TeamSheet(team_id=choir["choir"], file_id="f123"))
    await _request(choir["choir"], "nowhere")

    assert await drive_sync.apply(workdir) == 0

    sheet = await _sheet_row(choir["choir"])
    assert sheet.file_id == "f123", "the team keeps the sheet it already had"
    assert sheet.requested_file_id is None, (
        "dropped, not left to retry: the same link is just as invisible "
        "tomorrow, and a standing request would alert every night"
    )
    assert sheet.last_status == "error" and "not in the roster folder" in (
        sheet.last_error
    )
    assert "Choir" in (workdir / "alerts.txt").read_text()
    assert not (workdir / "out" / f"{NAME}.csv").exists(), (
        "the team sits out one night rather than syncing past its own rejection"
    )
    assert (workdir / "renames.txt").read_text() == ""


async def test_repoint_to_a_sheet_that_is_not_a_roster_is_refused(choir, tmp_path):
    """Checked even though this direction never reads the rows: overwriting the
    parish budget spreadsheet because someone pasted the wrong link is exactly
    what the layout check is for."""
    workdir = _workdir(tmp_path)
    _listing(workdir, [(NAME, "f123", NOW), ("parish-budget", "f999", NOW)])
    (workdir / "in" / f"{NAME}.csv").write_bytes(
        _sheet_csv(
            [["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"]]
        )
    )
    (workdir / "in" / "parish-budget.csv").write_bytes(
        b"Month,Envelope,Loose\r\nJanuary,1200,340\r\n"
    )
    await _request(choir["choir"], "f999")

    assert await drive_sync.apply(workdir) == 0

    sheet = await _sheet_row(choir["choir"])
    assert sheet.file_id != "f999", "not adopted"
    assert sheet.requested_file_id is None
    assert "header row" in sheet.last_error
    assert not (workdir / "out" / f"{NAME}.csv").exists()
    assert (workdir / "renames.txt").read_text() == "", (
        "nothing on Drive is renamed for a switch that did not happen"
    )


async def test_repoint_for_a_team_with_no_sheet_yet(choir, tmp_path):
    """No outgoing sheet means no parking rename — just the adoption."""
    workdir = _workdir(tmp_path)
    _listing(workdir, [("choir-copy", "f999", NOW)])
    (workdir / "in" / "choir-copy.csv").write_bytes(
        _sheet_csv(
            [["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"]]
        )
    )
    await _request(choir["choir"], "f999")

    assert await drive_sync.apply(workdir) == 0

    sheet = await _sheet_row(choir["choir"])
    assert sheet.file_id == "f999"
    assert (workdir / "renames.txt").read_text() == f"choir-copy\t{NAME}\n"
