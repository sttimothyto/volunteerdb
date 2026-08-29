"""Refresh every team home page from its public Google Doc (in-app
scheduler, VDB_FETCH_PAGES_AT).

Each team is fetched in its own transaction with its own error handling, so
one bad doc (deleted, made private, oversized) cannot poison the batch — it
records status='error' on its team_page row, keeps the last good HTML, and
the job carries on. Exit code 1 only for systemic failure (DB unreachable).

Usage: python -m volunteerdb.jobs.fetch_pages
"""

import asyncio
import sys

import httpx
import sqlalchemy as sa

from .. import env as env_mod
from ..db import db_session
from ..env import Env
from ..log import init_logging
from ..models import Team
from ..services import pages as page_service
from . import job_lock


async def main(env: Env) -> int:
    init_logging()
    async with db_session() as session:
        team_ids = list(
            await session.scalars(
                sa.select(Team.id)
                .where(Team.is_active, Team.home_doc_url.is_not(None))
                .order_by(Team.id)
            )
        )

    ok = failed = 0
    async with httpx.AsyncClient() as client:
        for team_id in team_ids:
            async with db_session() as session:
                team = await session.get(Team, team_id)
                if team is None or not team.home_doc_url:
                    continue  # changed while the job ran
                page = await page_service.fetch_and_store(session, team, client)
                if page.status == "ok":
                    ok += 1
                else:
                    failed += 1
                    print(f"FAILED {team.name!r}: {page.error}", file=sys.stderr)

    print(f"fetched {ok} team page(s), {failed} failure(s), {len(team_ids)} total")
    return 0


def cli() -> int:
    async def locked() -> int:
        async with job_lock("fetch_pages") as acquired:
            if not acquired:
                print("skipped: another fetch_pages run holds the job lock")
                return 0
            return await main(env_mod.build())

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
