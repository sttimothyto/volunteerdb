"""Point-in-time ("as of") query support over the system-versioned tables.

Each versioned table carries `sys_period tstzrange` (when this row version was
current) and archives closed versions into a `<table>_history` twin via the
`versioning()` trigger. The state as of time T is the union of live and history
rows whose period contains T.

IMPORTANT: historical entity rows must never enter the caller's Session —
the identity map would hand back live instances for historical PKs (and vice
versa), and a mutated snapshot could even be flushed to the database. That's
why `fetch()` runs as-of statements in a throwaway session and detaches every
result. Always execute entity-returning as-of statements through `fetch()`.
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .db import sessionmaker
from .models import HISTORY_TABLES, Base


def asof[T: Base](model: type[T], at: datetime) -> type[T]:
    """An aliased ORM entity representing `model` as of time `at`.

    Usable anywhere the model class is: select(asof(Volunteer, t)), joins, etc.
    Entities loaded through it are read-only snapshots; execute via fetch().
    """
    live = model.__table__
    hist = HISTORY_TABLES[model]
    ts = sa.literal(at, sa.TIMESTAMP(timezone=True))
    names = [c.name for c in live.columns]
    live_sel = sa.select(*[live.c[n] for n in names]).where(
        live.c.sys_period.contains(ts)
    )
    hist_sel = sa.select(*[hist.c[n] for n in names]).where(
        hist.c.sys_period.contains(ts)
    )
    subq = sa.union_all(live_sel, hist_sel).subquery(f"{live.name}_asof")
    return aliased(model, subq)  # type: ignore[return-value]


def entity[T: Base](model: type[T], at: datetime | None) -> type[T]:
    """`model` itself, or its as-of alias when `at` is given."""
    return asof(model, at) if at is not None else model


async def fetch(
    session: AsyncSession, stmt: sa.Select, at: datetime | None
) -> list[sa.Row]:
    """Execute an entity-returning statement, as-of safe.

    Live queries (at is None) use the caller's session as usual. As-of queries
    run in their own short-lived session and come back detached, so snapshots
    never collide with live identities or get accidentally flushed.
    """
    if at is None:
        return list((await session.execute(stmt)).all())
    async with sessionmaker()() as snapshot_session:
        rows = list((await snapshot_session.execute(stmt)).all())
        snapshot_session.expunge_all()
        return rows
