"""Nightly one-shot jobs.

fetch_pages, proposal_digest and event_reminders are driven in-process by
volunteerdb.scheduler at parish-local times from settings; each stays
runnable by hand via `python -m volunteerdb.jobs.<name>` (in production, a
one-shot app container). The drive_sync legs are different: they are invoked
by the host's systemd-timed sync script, which sandwiches them between
rclone transfers over a shared work dir.

Scheduler and CLI both take job_lock() around a run, so a manual run and the
in-app scheduler can never work the same job concurrently.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa

from ..db import engine


@asynccontextmanager
async def job_lock(name: str) -> AsyncIterator[bool]:
    """Try to take the cluster-wide advisory lock for job `name`.

    Yields whether the lock was acquired; the caller must skip the run on
    False. Held on a dedicated connection and always released before the
    connection returns to the pool (advisory locks are session-scoped).
    """
    key = sa.func.hashtext(f"volunteerdb.job.{name}")
    async with engine().connect() as conn:
        acquired = bool(await conn.scalar(sa.select(sa.func.pg_try_advisory_lock(key))))
        try:
            yield acquired
        finally:
            if acquired:
                await conn.execute(sa.select(sa.func.pg_advisory_unlock(key)))
