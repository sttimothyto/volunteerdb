"""Syncing a team's roster between the database and its Google Sheet.

This is the orchestration layer over services.gsheets: it owns the sessions,
the permission check, the bookkeeping on team_sheet, and the decision about
which direction wins. It lives apart from services.teams because both halves
of the CSV pipeline (sheets.importer, sheets.exporter) import services.teams,
so anything reaching for them cannot live there too.

Session ownership follows sheets.importer.run_import rather than the usual
service shape: the functions here take a user_id and open their own sessions,
because a sync interleaves database transactions with network calls and must
not hold a transaction open across them. user_id=None is a trusted internal
caller (the nightly job).

Direction, and why importing never removes anybody: a sheet is a convenience
surface that a leader edits by hand, with filters, sorted views and pasted
blocks. Treating a missing row as a resignation makes every clumsy edit a
data-loss event. So the importer only adds and updates -- there is no switch
for anything else -- and the write-back leg immediately puts anyone it did not
see back into the sheet. Removing a member is an action in the app, where it
is attributable and reversible.
"""

import csv
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO

from ..config import settings
from ..db import db_session
from ..log import audit_log
from ..models import AppUser, SyncStatus, TeamSheet
from ..permissions import load_actor, require
from ..services import gsheets
from ..services import pages as page_service
from ..services import teams as team_service
from ..services import users as user_service
from ..sheets import exporter, importer

IMPORT = "import"
EXPORT = "export"
SHEET_SUFFIX = "-membership-list"
# The bot account nightly writes are attributed to. Still "drive-sync" though
# rclone and Drive are long gone from this path: the account already owns every
# history row the sync has ever written, and renaming it would strand them
# behind a second, identical-looking user.
SYNC_USER_LOCALPART = "drive-sync"


@dataclass
class SyncOutcome:
    """What one team's sync did, for a toast, a job log line, and team_sheet."""

    status: str
    message: str
    report: importer.ImportReport | None = None
    wrote_sheet: bool = False

    @property
    def failed(self) -> bool:
        return self.status == SyncStatus.error


def sync_user_email() -> str:
    """History attribution for sync writes: a named account, never NULL.

    Derived from the sender domain rather than configured separately — one
    fewer thing to set, and it cannot end up on a domain the parish does not
    own. The mail_from validator guarantees the "@", so the split is total.
    The account can never sign in: random discarded password, deactivated.
    """
    return f"{SYNC_USER_LOCALPART}@{settings().mail_from.rsplit('@', 1)[1]}"


async def ensure_sync_user() -> int:
    email = sync_user_email()
    async with db_session() as session:
        user = await user_service.get_by_email(session, email)
        if user is None:
            user, _ = await user_service.create(
                session,
                email,
                password=secrets.token_urlsafe(32),
                link_by_email=False,  # a bot, never a volunteer's login
            )
            user.is_active = False
            await session.flush()
        return user.id


def same_rows(a: bytes, b: bytes) -> bool:
    """Logically-equal CSVs (quoting/line-ending/BOM differences ignored):
    used to skip writes that would only churn the sheet's version history."""
    try:
        return list(csv.reader(StringIO(a.decode("utf-8-sig")))) == list(
            csv.reader(StringIO(b.decode("utf-8-sig")))
        )
    except UnicodeDecodeError:
        return False


async def record_status(
    team_id: int, status: str, error: str | None, *, synced: bool = False
) -> None:
    """Stamp the outcome on team_sheet. last_synced_at only advances on a
    clean run, so a failed sheet still reads as needing attention."""
    async with db_session() as session:
        sheet = await session.get(TeamSheet, team_id)
        if sheet is None:
            sheet = TeamSheet(team_id=team_id)
            session.add(sheet)
        sheet.last_status = status
        sheet.last_error = error
        if synced:
            sheet.last_synced_at = datetime.now(UTC)


async def team_dropdown_values(team_id: int) -> list[str] | None:
    """Values for the sheet's Team column: exactly this team's display path.

    A team's own sheet manages one team — the importer rejects a row naming
    anywhere else — so offering anything wider would only invite a row the
    sync must refuse.
    """
    async with db_session() as session:
        tree = await team_service.tree(session)
        path = tree.paths.get(team_id)
    return None if path is None else [path]


async def sheet_title(team_id: int) -> str:
    async with db_session() as session:
        tree = await team_service.tree(session)
        paths = tree.paths
        slug = page_service.slug_map(paths).get(team_id, str(team_id))
    return f"{slug}{SHEET_SUFFIX}"


async def create_for_team(token: str, team_id: int) -> str:
    """Make a team its own roster sheet, shared and decorated, and store it.

    Sharing is set here rather than left to the folder: link-editable is what
    the whole feature runs on, and a sheet that reached a leader unshared
    would simply look broken to them.
    """
    title = await sheet_title(team_id)
    file_id = await gsheets.create_sheet(token, title)
    await gsheets.share_anyone_writer(token, file_id)
    async with db_session() as session:
        sheet = await session.get(TeamSheet, team_id)
        if sheet is None:
            sheet = TeamSheet(team_id=team_id)
            session.add(sheet)
        sheet.file_id = file_id
        sheet.file_name = title
    audit_log("roster_sheet.created", team_id=team_id, file_id=file_id)
    return file_id


async def import_sheet(
    file_id: str, team_id: int, user_id: int
) -> importer.ImportReport:
    """Sheet → database. The importer cannot remove anybody at all — see the
    module docstring for why that is the design and not a setting."""
    content = await gsheets.read_csv(file_id)
    return await importer.run_team_sync(content, team_id=team_id, user_id=user_id)


async def export_sheet(token: str, file_id: str, team_id: int) -> bool:
    """Database → sheet; True if anything was actually written.

    subtree=False: a team's sheet is its own direct memberships. actor=None
    because the sync is a trusted internal caller — it must see the contact
    columns it is about to write back.
    """
    async with db_session() as session:
        content = await exporter.export_csv(
            session, None, team_id=team_id, subtree=False
        )
    try:
        current = await gsheets.read_csv(file_id)
    except gsheets.GSheetsError:
        current = b""  # unreadable is not a reason to skip the write
    if current and same_rows(current, content):
        return False
    await gsheets.write_rows(token, file_id, content)
    return True


async def sync_team(
    team_id: int, *, direction: str, user_id: int | None
) -> SyncOutcome:
    """One team's sync, in the direction asked for.

    IMPORT applies the sheet's rows and then writes the result back, so the
    sheet ends up showing exactly what the database now holds — including the
    people the sheet had dropped. EXPORT skips the read entirely and
    overwrites the sheet from the database: the answer that cannot lose parish
    data, and the right one when a leader has made a mess of the file.

    A failed import deliberately does NOT write back: the leader's edits stay
    in the sheet to be corrected, rather than being replaced by the very rows
    they were trying to change.
    """
    if not gsheets.enabled():
        raise ValueError(
            "roster spreadsheet sync is not configured — set the VDB_SHEETS_* "
            "settings (docs/how-to/roster-spreadsheets.md)"
        )
    if direction not in (IMPORT, EXPORT):
        raise ValueError(f"unknown sync direction {direction!r}")

    async with db_session(user_id) as session:
        actor = None
        if user_id is not None:
            user = await session.get(AppUser, user_id)
            assert user is not None, f"unknown user {user_id}"
            actor = await load_actor(session, user)
        require(
            actor is None or actor.can_manage_team(team_id),
            "sync this team's roster spreadsheet",
        )
        sheet = await session.get(TeamSheet, team_id)
        file_id = sheet.file_id if sheet else None
        if not file_id:
            raise LookupError("this team has no roster spreadsheet linked yet")

    # Writes are attributed to the bot account, not to whoever pressed the
    # button: the rows come from the sheet, and a leader who synced somebody
    # else's typo should not appear in history as its author.
    writer_id = await ensure_sync_user()
    report: importer.ImportReport | None = None

    try:
        if direction == IMPORT:
            report = await import_sheet(file_id, team_id, writer_id)
            if not report.applied:
                message = (
                    "; ".join(
                        f"row {issue.row}: {issue.message}"
                        for issue in report.errors[:5]
                    )
                    or "the sheet could not be read"
                )
                await record_status(team_id, SyncStatus.error, message)
                return SyncOutcome(SyncStatus.error, message, report)
        token = await gsheets.mint_token()
        wrote = await export_sheet(token, file_id, team_id)
        await gsheets.decorate(token, file_id, await team_dropdown_values(team_id))
    except gsheets.GSheetsError as exc:
        await record_status(team_id, SyncStatus.error, str(exc))
        return SyncOutcome(SyncStatus.error, str(exc), report)

    if direction == IMPORT:
        message = (
            f"+{report.volunteers_created} volunteers, "
            f"+{report.memberships_created}/~{report.memberships_updated} "
            "memberships; sheet rewritten to match"
        )
    else:
        message = (
            "sheet rewritten from the database" if wrote else "sheet already matched"
        )
    status = (
        SyncStatus.applied if wrote or direction == IMPORT else SyncStatus.unchanged
    )
    await record_status(team_id, status, None, synced=True)
    audit_log("roster_sheet.synced", team_id=team_id, direction=direction)
    return SyncOutcome(status, message, report, wrote_sheet=wrote)
