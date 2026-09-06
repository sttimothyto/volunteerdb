"""Nightly one-shot jobs.

fetch_pages, proposal_digest and event_reminders are driven in-process by
volunteerdb.scheduler at parish-local times from settings; each stays
runnable by hand via `python -m volunteerdb.jobs.<name>` (in production, a
one-shot app container). Each job is read -> plan -> execute where a plan is
worth having: the reads pull plain values, the plan is a pure function the
tests drive without a database, and the executor performs the sends and
stamps through the Env.

Scheduler and CLI both take job_lock() around a run, so a manual run and the
in-app scheduler can never work the same job concurrently: a job's cli() is
`run_locked(name, main)`. The two nightly digests send through
`send_digests`, which is the one place a digest is mailed and its stamps
recorded.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import sqlalchemy as sa

from .. import env as env_mod
from ..db import transaction

if TYPE_CHECKING:
    from sqlalchemy.sql import Executable

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


def run_locked(name: str, main: Callable[[Env], Awaitable[int]]) -> int:
    """A job's command line: build the process Env, take the job's lock, run
    `main` under it and answer its exit status -- or 0 with a note when
    another run (the scheduler's, or a second hand) holds the lock."""

    async def locked() -> int:
        env = env_mod.build()
        async with job_lock(env, name) as acquired:
            if not acquired:
                print(f"skipped: another {name} run holds the job lock")
                return 0
            return await main(env)

    return asyncio.run(locked())


class Digest(Protocol):
    """What send_digests needs of a job's digest: whom it is for."""

    email: str


async def send_digests[D: Digest](
    digests: Sequence[D],
    env: Env,
    *,
    render: Callable[[D], tuple[str, str]],
    stamp: Callable[[D], Executable],
) -> JobReport:
    """Send one mail per digest through the Env's mailer and, once it went,
    run its `stamp` statement in a transaction of its own: a failed send
    records nothing and is retried the next night. `render` is the digest's
    subject and body; `stamp` the insert that records what it settled."""
    sent = failed = 0
    for digest in digests:
        subject, body = render(digest)
        if not await env.mailer.send(digest.email, subject, body):
            failed += 1
            print(f"FAILED digest to {digest.email}", file=sys.stderr)
            continue
        async with transaction(env, None) as session:
            await session.execute(stamp(digest))
        sent += 1
    return JobReport(sent=sent, failed=failed)
