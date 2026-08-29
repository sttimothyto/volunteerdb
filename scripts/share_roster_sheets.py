"""One-shot: share every existing roster sheet as "anyone with the link can edit".

The 67 sheets the retired rclone sync created were shared per-leader, by
individual Drive grants. The link-shared model replaces that: whoever holds
the link edits the sheet, and the parish token writes it back. Until a sheet
is link-shared, `services.gsheets.read_csv` gets a sign-in page instead of the
roster and the team's sync reports "the spreadsheet is not shared".

Run once, after VDB_SHEETS_* is set and before the first nightly run:

    uv run python scripts/share_roster_sheets.py --dry-run
    uv run python scripts/share_roster_sheets.py

It reads team_sheet.file_id -- so it shares exactly the sheets this system
actually syncs, not everything sitting in the folder -- and is idempotent:
Drive returns the existing grant rather than a second one, and a sheet already
readable is reported and skipped.

Requires the OAuth client that CREATED those sheets (the rclone one), because
drive.file is scoped per client per file. With any other client every call
here 404s.

The per-leader grants already on the sheets are left alone. They are harmless
once the link works, and removing access is not something a migration script
should do unattended.
"""

import argparse
import asyncio
import sys

import httpx
import sqlalchemy as sa

from volunteerdb import env as env_mod
from volunteerdb.db import db_session, init
from volunteerdb.errors import External
from volunteerdb.fp import Err, Ok, Result
from volunteerdb.log import init_logging
from volunteerdb.models import TeamSheet
from volunteerdb.services import gsheets
from volunteerdb.services import teams as team_service


async def _sheets() -> list[tuple[str, str]]:
    """(file_id, team path) for every team that has a sheet."""
    async with db_session() as session:
        tree = await team_service.tree(session)
        rows = (await session.execute(sa.select(TeamSheet))).scalars()
        return [
            (s.file_id, tree.paths.get(s.team_id, f"team {s.team_id}"))
            for s in rows
            if s.file_id
        ]


async def _is_public(
    client: httpx.AsyncClient, token: str, file_id: str
) -> Result[bool, External]:
    """Does an anyone-with-link grant already exist?"""
    resp = await client.get(
        f"{gsheets.DRIVE_API}/{file_id}/permissions",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": "permissions(id,type,role)"},
    )
    if resp.status_code != 200:
        return Err(
            External(gsheets.SERVICE, f"HTTP {resp.status_code} listing permissions")
        )
    return Ok(
        any(
            p.get("type") == "anyone" and p.get("role") in ("writer", "owner")
            for p in resp.json().get("permissions", [])
        )
    )


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be shared, change nothing",
    )
    args = parser.parse_args(argv)

    init_logging()
    env = env_mod.build()
    cfg = env.google()
    if not gsheets.enabled(cfg):
        print("FATAL: VDB_SHEETS_* is not configured", file=sys.stderr)
        return 1
    init(env.settings.database_url)

    sheets = await _sheets()
    print(f"{len(sheets)} sheet(s) on file")

    shared = already = failed = 0
    async with env.http.client(timeout=gsheets.TIMEOUT) as client:
        minted = await gsheets.mint_token(client, cfg)
        if isinstance(minted, Err):
            print(f"FATAL: {minted.error.message}", file=sys.stderr)
            return 1
        token = minted.value
        for file_id, path in sheets:
            try:
                public = await _is_public(client, token, file_id)
            except httpx.HTTPError as exc:
                failed += 1
                print(f"  FAILED   {path}: {exc}", file=sys.stderr)
                continue
            if isinstance(public, Err):
                failed += 1
                print(f"  FAILED   {path}: {public.error.message}", file=sys.stderr)
                continue
            if public.value:
                already += 1
                print(f"  ok       {path}")
                continue
            if args.dry_run:
                print(f"  would    {path}")
                shared += 1
                continue
            done = await gsheets.share_anyone_writer(client, token, file_id)
            if isinstance(done, Err):
                failed += 1
                print(f"  FAILED   {path}: {done.error.message}", file=sys.stderr)
                continue
            shared += 1
            print(f"  SHARED   {path}")

    verb = "would share" if args.dry_run else "shared"
    print(f"{verb} {shared}, already shared {already}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
