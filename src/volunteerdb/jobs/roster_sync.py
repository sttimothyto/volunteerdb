"""Sync every active team's roster with its Google Sheet (in-app scheduler,
VDB_ROSTER_SYNC_AT).

This replaces the old two-machine drive_sync: rclone on the host, a
bind-mounted work dir, renames.txt, manifest.json and a systemd timer. All of
that existed to work around a drive.file OAuth grant that could only see files
this system created. Roster sheets are now shared "anyone with the link can
edit", so the app reads them anonymously over the CSV export endpoint and
writes them with the parish account's token — no host leg left to coordinate.

Each team is synced independently: one unreadable or unshared sheet records
status='error' against its own team_sheet row and the job carries on. Exit
code 1 only for systemic failure (not configured, DB unreachable).

A team with no sheet yet gets one made for it, shared and decorated, then
filled from the database.

Usage: python -m volunteerdb.jobs.roster_sync
"""

import asyncio
import sys

import sqlalchemy as sa

from .. import env as env_mod
from ..db import db_session
from ..env import Env
from ..errors import External, message
from ..fp import Err, Result
from ..log import init_logging
from ..models import SyncStatus, TeamSheet
from ..services import google_api, gsheets
from ..services import roster_sheets as sheet_service
from ..services import teams as team_service
from . import job_lock


async def _syncable_teams() -> list[tuple[int, str, str | None]]:
    """(team_id, display path, file_id) for every team that should have a sheet.

    A task-force meta team is a borrowed roster, not a ministry with its own
    sheet: syncing one would publish the collaborating teams' contact details
    into a link-shared file and let a sheet editor rewrite borrowed members'
    addresses — exactly the escalation actors.load_actor cuts out. Never
    give one a sheet.
    """
    async with db_session() as session:
        tree = await team_service.tree(session)
        meta = await team_service.meta_team_ids(session)
        stored = {
            s.team_id: s.file_id
            for s in (await session.execute(sa.select(TeamSheet))).scalars()
        }
        return [
            (t.id, tree.paths[t.id], stored.get(t.id))
            for t in tree.teams
            if t.is_active and t.id not in meta
        ]


async def _create(
    env: Env, cfg: google_api.GoogleConfig, team_id: int
) -> Result[str, External]:
    """A sheet for a team that has none: a token, then the service makes,
    shares and records it."""
    async with env.http.client(timeout=gsheets.TIMEOUT) as client:
        token = await gsheets.mint_token(client, cfg)
    if isinstance(token, Err):
        return token
    return await sheet_service.create_for_team(env, token.value, team_id)


async def main(env: Env) -> int:
    init_logging()
    cfg = env.google()
    if not gsheets.enabled(cfg):
        print("roster_sync: not configured (VDB_SHEETS_*) — nothing to do")
        return 0

    teams = await _syncable_teams()
    counts = {"synced": 0, "created": 0, "failed": 0}

    for team_id, path, file_id in teams:
        try:
            if not file_id:
                made = await _create(env, cfg, team_id)
                if isinstance(made, Err):
                    counts["failed"] += 1
                    await sheet_service.record_status(
                        env, team_id, SyncStatus.error, made.error.message
                    )
                    print(f"FAILED {path}: {made.error.message}", file=sys.stderr)
                    continue
                counts["created"] += 1
                # a brand-new sheet has nothing in it worth importing, so the
                # first pass is always database -> sheet
                direction = sheet_service.EXPORT
            else:
                direction = sheet_service.IMPORT
            synced = await sheet_service.sync_team(
                env, team_id, direction=direction, user_id=None, now=env.clock.now()
            )
        except Exception as exc:  # one team's problem is not the job's
            counts["failed"] += 1
            await sheet_service.record_status(env, team_id, SyncStatus.error, str(exc))
            print(f"FAILED {path}: {exc}", file=sys.stderr)
            continue
        if isinstance(synced, Err):
            counts["failed"] += 1
            text = message(synced.error)
            await sheet_service.record_status(env, team_id, SyncStatus.error, text)
            print(f"FAILED {path}: {text}", file=sys.stderr)
            continue
        outcome = synced.value
        if outcome.failed:
            counts["failed"] += 1
            print(f"FAILED {path}: {outcome.message}", file=sys.stderr)
        else:
            counts["synced"] += 1
            print(f"{path}: {outcome.message}")

    print(
        f"roster sync: {counts['synced']} synced, {counts['created']} created, "
        f"{counts['failed']} failed of {len(teams)} teams"
    )
    return 0


def cli() -> int:
    async def locked() -> int:
        async with job_lock("roster_sync") as acquired:
            if not acquired:
                print("skipped: another roster_sync run holds the job lock")
                return 0
            return await main(env_mod.build())

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
