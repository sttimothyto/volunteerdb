from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init(url: str | None = None) -> AsyncEngine:
    """Create (or replace) the global engine. Tests call this with their own URL."""
    global _engine, _sessionmaker
    # pre_ping: validate pooled connections so a Postgres restart doesn't
    # surface as one error per stale connection
    _engine = create_async_engine(url or settings().database_url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> AsyncEngine:
    return _engine if _engine is not None else init()


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def db_session(user_id: int | None = None) -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work; commits on clean exit, rolls back on error.

    `user_id` is recorded transaction-locally so the history triggers can stamp
    `changed_by` on every row they archive.
    """
    with bind_fallback_user(user_id):
        async with sessionmaker()() as session:
            async with session.begin():
                if user_id is not None:
                    await session.execute(
                        select(func.set_config("app.user_id", str(user_id), True))
                    )
                yield session
