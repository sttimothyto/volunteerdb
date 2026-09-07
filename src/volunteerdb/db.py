from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, Integer, any_, func, literal, select
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
from .log import bind_fallback_user

if TYPE_CHECKING:
    from .env import Env


def any_of(column, ids: "Iterable[int]"):
    """``column = ANY(:ids)`` -- one array bind, where ``column.in_(ids)`` is
    one bind per element.

    SQLAlchemy renders an ``IN`` list as an *expanding* parameter: the
    placeholders are re-rendered on every execution, so a query over the whole
    parish pays an O(n) string build each time it runs and ships n parameters
    to the driver. The dashboard was rendering 25,000 of them per load. The
    Postgres array form is one parameter whose length never changes the
    statement, so the render happens once and the compiled-statement cache is
    hit whatever the list length.

    Only worth it for wide lists (hundreds up); ``in_()`` stays clearer for a
    handful. An empty list is a legal empty array and matches nothing, exactly
    as ``in_([])`` does.
    """
    return column == any_(literal(list(ids), ARRAY(Integer)))


def make_engine(url: str) -> AsyncEngine:
    # pre_ping: validate pooled connections so a Postgres restart doesn't
    # surface as one error per stale connection
    return create_async_engine(url, pool_pre_ping=True)


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: what a service returned stays readable after the
    # edge commits, which is what lets a page render and a policy mail from it
    return async_sessionmaker(engine, expire_on_commit=False)


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
