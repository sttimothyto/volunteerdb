"""Nightly one-shot jobs.

fetch_pages, proposal_digest and event_reminders are driven in-process by
volunteerdb.scheduler at parish-local times from settings; each stays
runnable by hand via `python -m volunteerdb.jobs.<name>` (in production, a
one-shot app container). Each job is read -> plan -> execute where a plan is
worth having: the reads pull plain values, the plan is a pure function the
tests drive without a database, and the executor performs the sends and
stamps through the Env.

Scheduler and CLI both take job_lock() around a run, so a manual run and the
in-app scheduler can never work the same job concurrently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa

if TYPE_CHECKING:
    from ..env import Env


@dataclass(frozen=True)
class JobReport:
    """What a run did: how many it reached, how many it could not."""

    sent: int = 0
    failed: int = 0


@asynccontextmanager
async def job_lock(env: Env, name: str) -> AsyncIterator[bool]:
    """Try to take the cluster-wide advisory lock for job `name`.

    Yields whether the lock was acquired; the caller must skip the run on
    False. Held on a dedicated connection of the Env's engine and always
    released before the connection returns to the pool (advisory locks are
    session-scoped).
    """
    key = sa.func.hashtext(f"volunteerdb.job.{name}")
    async with env.engine.connect() as conn:
        acquired = bool(await conn.scalar(sa.select(sa.func.pg_try_advisory_lock(key))))
        try:
            yield acquired
        finally:
            if acquired:
                await conn.execute(sa.select(sa.func.pg_advisory_unlock(key)))
