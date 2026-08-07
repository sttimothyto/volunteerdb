"""Two-way Google Drive roster sync (02:30 cron).

File-based by design: rclone on the HOST talks to Drive (reusing the backup
remote's OAuth); this job only reads and writes a bind-mounted work dir, so
the whole Python side is testable without Drive.

Work-dir contract (host side: deploy/templates/volunteerdb-drive-sync.sh.j2):
    in/listing.json    rclone lsjson of the Drive folder (Name, ID, ModTime)
    in/<name>.csv      each Google Sheet exported as CSV
    out/<name>.csv     regenerated rosters to upload (converted back to Sheets)
    renames.txt        "<old>TAB<new>" Drive names where a team's slug changed
    alerts.txt         human-readable problems; non-empty triggers the email
    post/listing.json  listing taken after the upload, read by `record`

Subcommands:
    apply  <workdir>   apply sheet edits to Postgres, regenerate out/*.csv
    record <workdir>   read post/listing.json, store file ids + sync marks

Per team, `apply` is all-or-nothing (importer.run_team_sync); a sheet that
fails to parse — or trips the removal safety threshold — is skipped, recorded
on its team_sheet row and alerted, and its out/*.csv is NOT regenerated: the
leader's edits stay on Drive to be fixed, never silently overwritten. A sheet
whose Drive ModTime has not advanced past team_sheet.last_synced_at skips the
apply leg entirely, so the database wins unless the sheet was actually edited.
"""

import asyncio
import csv
import json
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path

import sqlalchemy as sa

from ..db import db_session
from ..log import init_logging
from ..models import TeamSheet
from ..services import pages as page_service
from ..services import teams as team_service
from ..services import users as user_service
from ..sheets import exporter, importer

SHEET_SUFFIX = "-membership-list"
MAX_SHEET_BYTES = 5_000_000  # a roster CSV is KBs; anything near this is wrong
# History attribution: sync writes carry a named account, not NULL. The
# account can never sign in — random discarded password, deactivated.
SYNC_USER_EMAIL = "drive-sync@sttimothyto.org"


@dataclass
class DriveEntry:
    name: str  # Drive document name (rclone's export .csv suffix stripped)
    file_id: str
    mod_time: datetime


def _parse_time(raw: str) -> datetime:
    # rclone emits RFC3339 with up to nanosecond precision; fromisoformat
    # takes at most microseconds
    return datetime.fromisoformat(re.sub(r"\.(\d{6})\d+", r".\1", raw))


def load_listing(path: Path) -> list[DriveEntry]:
    entries = []
    for item in json.loads(path.read_text()):
        if item.get("IsDir"):
            continue
        name = item["Name"]
        name = name.removesuffix(".csv")
        entries.append(DriveEntry(name, item["ID"], _parse_time(item["ModTime"])))
    return entries


def _same_rows(a: bytes, b: bytes) -> bool:
    """Logically-equal CSVs (quoting/line-ending/BOM differences ignored):
    used to skip uploads that would only churn the sheet's version history."""
    try:
        return list(csv.reader(StringIO(a.decode("utf-8-sig")))) == list(
            csv.reader(StringIO(b.decode("utf-8-sig")))
        )
    except UnicodeDecodeError:
        return False


async def _ensure_sync_user() -> int:
    async with db_session() as session:
        user = await user_service.get_by_email(session, SYNC_USER_EMAIL)
        if user is None:
            user = await user_service.create(
                session,
                SYNC_USER_EMAIL,
                password=secrets.token_urlsafe(32),
                link_by_email=False,  # a bot, never a volunteer's login
            )
            user.is_active = False
            await session.flush()
        return user.id


async def _active_teams() -> tuple[list[tuple[int, str, str]], dict[int, tuple]]:
    """(team_id, display path, expected Drive name) for active teams, plus
    each team's stored sheet identity (file_id, last_synced_at, last_status)."""
    async with db_session() as session:
        all_teams = await team_service.list_all(session)
        paths = team_service.team_paths(all_teams)
        slugs = page_service.slug_map(paths)
        active = [
            (t.id, paths[t.id], slugs[t.id] + SHEET_SUFFIX)
            for t in all_teams
            if t.is_active
        ]
        stored = {
            s.team_id: (s.file_id, s.last_synced_at, s.last_status)
            for s in (await session.execute(sa.select(TeamSheet))).scalars()
        }
    return active, stored


async def _set_sheet_status(team_id: int, status: str, error: str | None) -> None:
    async with db_session() as session:
        sheet = await session.get(TeamSheet, team_id)
        if sheet is None:
            sheet = TeamSheet(team_id=team_id)
            session.add(sheet)
        sheet.last_status = status
        sheet.last_error = error


async def apply(workdir: Path) -> int:
    init_logging()
    in_dir, out_dir = workdir / "in", workdir / "out"
    listing_path = in_dir / "listing.json"
    if not listing_path.exists():
        print(
            f"FATAL: {listing_path} missing — rclone leg did not run", file=sys.stderr
        )
        return 1
    entries = load_listing(listing_path)
    by_id = {e.file_id: e for e in entries}
    by_name = {e.name: e for e in entries}

    sync_user_id = await _ensure_sync_user()
    active, stored = await _active_teams()

    out_dir.mkdir(parents=True, exist_ok=True)
    alerts: list[str] = []
    renames: list[tuple[str, str]] = []
    counts = {"applied": 0, "unchanged": 0, "new": 0, "error": 0}

    matched_ids: set[str] = set()
    for team_id, path, expected in active:
        file_id, last_synced, _ = stored.get(team_id, (None, None, None))
        entry = by_id.get(file_id) if file_id else None
        if entry is None:
            entry = by_name.get(expected)
        if entry is not None:
            matched_ids.add(entry.file_id)
            if entry.name != expected:
                renames.append((entry.name, expected))

        status: str
        error: str | None = None
        if entry is None:
            status = "new"  # no sheet on Drive yet; the regenerate leg creates it
        elif last_synced is not None and entry.mod_time <= last_synced:
            status = "unchanged"  # nobody edited the sheet since our last upload
        else:
            csv_path = in_dir / f"{entry.name}.csv"
            if not csv_path.exists():
                status = "error"
                error = f"sheet {entry.name!r} is listed but its CSV export is missing"
            elif csv_path.stat().st_size > MAX_SHEET_BYTES:
                status = "error"
                error = f"sheet {entry.name!r} export is implausibly large"
            else:
                report = await importer.run_team_sync(
                    csv_path.read_bytes(), team_id=team_id, user_id=sync_user_id
                )
                if report.applied:
                    status = "applied"
                    print(
                        f"{path}: +{report.volunteers_created} volunteers, "
                        f"+{report.memberships_created}/~{report.memberships_updated}"
                        f"/-{report.memberships_removed} memberships, "
                        f"{report.volunteers_archived} archived, "
                        f"{report.volunteers_reactivated} reactivated"
                    )
                    if report.churn_suspected:
                        # applied, but worth a human look: the alert email
                        # fires on any non-empty alerts.txt, error or not
                        alerts.append(
                            f"{path}: WARNING — sync created "
                            f"{report.volunteers_created} volunteer(s) while "
                            f"removing {report.memberships_removed} member(s); "
                            "check the roster for a duplicated person"
                        )
                else:
                    status = "error"
                    error = "; ".join(
                        f"row {issue.row}: {issue.message}"
                        for issue in report.errors[:5]
                    )
        counts[status] += 1
        if error is not None:
            alerts.append(f"{path}: {error}")
            print(f"FAILED {path}: {error}", file=sys.stderr)
        await _set_sheet_status(team_id, status, error)

        if status == "error":
            continue  # leave the leader's sheet on Drive untouched, to be fixed
        async with db_session() as session:
            content = await exporter.export_csv(session, team_id=team_id, subtree=False)
        in_path = in_dir / f"{entry.name}.csv" if entry else None
        if (
            in_path is not None
            and in_path.exists()
            and _same_rows(in_path.read_bytes(), content)
        ):
            continue  # nothing changed on either side: skip the upload churn
        (out_dir / f"{expected}.csv").write_bytes(content)

    for entry in entries:
        if entry.file_id not in matched_ids:
            print(f"NOTE: sheet {entry.name!r} matches no active team — left alone")

    (workdir / "renames.txt").write_text(
        "".join(f"{old}\t{new}\n" for old, new in renames)
    )
    (workdir / "alerts.txt").write_text("".join(f"{line}\n" for line in alerts))
    print(
        f"sync apply: {counts['applied']} applied, {counts['unchanged']} unchanged, "
        f"{counts['new']} new, {counts['error']} failed of {len(active)} teams"
    )
    return 0


async def record(workdir: Path) -> int:
    """Post-upload bookkeeping: capture each sheet's Drive id and ModTime.
    last_synced_at is NOT advanced for teams whose apply failed — their
    ModTime must stay 'newer than last sync' so the next night retries."""
    init_logging()
    listing_path = workdir / "post" / "listing.json"
    if not listing_path.exists():
        print(
            f"FATAL: {listing_path} missing — upload leg did not run", file=sys.stderr
        )
        return 1
    by_name = {e.name: e for e in load_listing(listing_path)}
    active, stored = await _active_teams()

    recorded = 0
    async with db_session() as session:
        for team_id, _path, expected in active:
            entry = by_name.get(expected)
            if entry is None:
                continue
            sheet = await session.get(TeamSheet, team_id)
            if sheet is None:
                sheet = TeamSheet(team_id=team_id)
                session.add(sheet)
            sheet.file_id = entry.file_id
            sheet.file_name = entry.name
            if sheet.last_status != "error":
                sheet.last_synced_at = entry.mod_time
            recorded += 1
    print(f"sync record: {recorded} sheet identities stored")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("apply", "record"):
        print(
            "usage: python -m volunteerdb.jobs.drive_sync {apply|record} <workdir>",
            file=sys.stderr,
        )
        return 2
    workdir = Path(argv[1])
    if argv[0] == "apply":
        return asyncio.run(apply(workdir))
    return asyncio.run(record(workdir))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
