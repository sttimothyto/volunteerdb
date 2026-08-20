"""Session-scoped memo of the team tree, dropped by SQLAlchemy session events.

The team table is a few dozen rows, and one request used to read it up to four
times: `permissions.load_actor` expands an actor's roles over the tree on every
authenticated request, then each service a page composes read it again, then the
page body read it once more. Reading it once per session is a plain win — but
only if it can never be stale, so the memo is dropped by a listener rather than
by its callers. `services/pages.py` sets ``team.home_doc_url`` without going
anywhere near `services/teams.py`; an explicit ``invalidate()`` there is exactly
what a later change forgets. Same argument as the history triggers: the
mechanism cannot be forgotten because nobody has to remember it
(docs/explanation/architecture.md, "Cross-cutting mechanisms").

The carrier is ``session.info``, which audit.py already uses for its
per-transaction state, and which `AsyncSession` shares with the `Session`
underneath it — so a listener popping the key here is immediately visible to the
async caller.

This module is deliberately top-level and imports no service. db.py imports it
for its listeners the way it imports audit; owning the memo inside
services/teams.py instead would close an import cycle, since that module reaches
db.py through history.py. It knows nothing about the memo's shape, only when it
dies.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, UOWTransaction

from .models import Team

if TYPE_CHECKING:  # the shape belongs to services.teams; importing it here cycles
    from .services.teams import TeamTree

# One entry per `at` snapshot, so a point-in-time tree is never served for a live
# read. audit.py owns "audit_txn" and "audit_writes"; this is the third key.
_KEY = "team_tree"


# AsyncSession.info IS the info dict of the Session underneath it, so the
# listeners below (which are handed the sync Session) and the service calls above
# (which hold the AsyncSession) are reading and writing the same mapping.
type AnySession = Session | AsyncSession


def cached(session: AnySession, at: datetime | None) -> "TeamTree | None":
    return session.info.get(_KEY, {}).get(at)


def store(session: AnySession, at: datetime | None, tree: "TeamTree") -> None:
    session.info.setdefault(_KEY, {})[at] = tree


def invalidate(session: AnySession) -> None:
    """Forget every snapshot, not just the live one.

    asof_param.parse_as_of bumps a bare date to the last microsecond of that day,
    so `?as_of=<today>` is a *future* timestamp whose live∪history union reads
    live rows. An as-of entry can therefore reflect the very rows just written,
    which makes "drop the key that matches" wrong and "drop them all" right. The
    cost is one small SELECT on a path that only runs after a team write.
    """
    session.info.pop(_KEY, None)


@event.listens_for(Session, "after_flush")
def _invalidate_on_team_write(session: Session, flush_context: UOWTransaction) -> None:
    # No is_modified() filter, unlike audit.py's listener: over-invalidating costs
    # one small SELECT, while under-invalidating serves a tree that predates the
    # caller's own write.
    if any(
        isinstance(obj, Team)
        for obj in (*session.new, *session.dirty, *session.deleted)
    ):
        invalidate(session)


@event.listens_for(Session, "after_rollback")
def _invalidate_on_rollback(session: Session) -> None:
    # A rolled-back create would otherwise leave a phantom team in a memo the
    # session could still be read through.
    invalidate(session)
