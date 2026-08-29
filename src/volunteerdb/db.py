from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from . import (
    audit,  # noqa: F401 — registers the CRUD audit listeners
    team_cache,  # noqa: F401 — registers the team-tree cache listeners
)
from .config import settings
from .log import bind_fallback_user

if TYPE_CHECKING:
    from .env import Env

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str) -> AsyncEngine:
    # pre_ping: validate pooled connections so a Postgres restart doesn't
    # surface as one error per stale connection
    return create_async_engine(url, pool_pre_ping=True)


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: what a service returned stays readable after the
    # edge commits, which is what lets a page render and a policy mail from it
    return async_sessionmaker(engine, expire_on_commit=False)


def init(url: str | None = None) -> AsyncEngine:
    """Create (or replace) the global engine. Tests call this with their own URL."""
    global _engine, _sessionmaker
    _engine = make_engine(url or settings().database_url)
    _sessionmaker = make_sessions(_engine)
    return _engine


def engine() -> AsyncEngine:
    return _engine if _engine is not None else init()


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def _unit_of_work(
    sessions: async_sessionmaker[AsyncSession], user_id: int | None
) -> AsyncIterator[AsyncSession]:
    with bind_fallback_user(user_id):
        async with sessions() as session:
            async with session.begin():
                if user_id is not None:
                    await session.execute(
                        select(func.set_config("app.user_id", str(user_id), True))
                    )
                yield session


@asynccontextmanager
async def transaction(env: "Env", user_id: int | None) -> AsyncIterator[AsyncSession]:
    """One unit of work on the Env's engine. Commits on clean exit and rolls
    back when an exception unwinds it -- and ALSO when the block called
    ``await session.rollback()`` itself: after that the transaction is no
    longer active and the exit commits nothing (tests/test_uow.py), which is
    how an edge aborts on a returned Err with no exception in sight. A
    constraint violation still propagates: the transaction is dead, and the
    boundary maps it to Conflict.

    `user_id` is recorded transaction-locally so the history triggers can stamp
    `changed_by` on every row they archive.
    """
    async with _unit_of_work(env.sessions, user_id) as session:
        yield session


@asynccontextmanager
async def db_session(user_id: int | None = None) -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work on the process-global engine; the
    same contract as transaction(). Scripts, jobs and tests still reach the
    database through here; the global goes with them (FUNCTIONAL_REFACTORING.md,
    Phase 7)."""
    async with _unit_of_work(sessionmaker(), user_id) as session:
        yield session
