"""Google Calendar reconcile (in-app scheduler, every 30 minutes).

One-way, VolunteerDB → the public parish calendar. Each run first makes
sure the calendar exists and is shared the one way it should be (public
read, parish-owned, nobody else writing — services/gcal.verify_readonly),
then converges the calendar onto the database: scheduled events are
inserted, or patched when their payload fingerprint drifted; cancelled
events are deleted; a sweep deletes managed calendar entries whose VDB
event no longer exists (a team CASCADE is the only path there — no API
deletes events); and a last pass counts entries somebody typed into the
calendar by hand, which are reported and left alone. Nothing pushes from
the create/edit handlers on purpose: the sync is the single writer, and
≤30 minutes from edit to calendar is the documented deal. Each converged
event stamps its own row (google_event_id + google_fingerprint), so a
mid-run crash re-converges next run.

The calendar itself is created on the first run with a token and its id
kept in app_setting; a calendar somebody deleted in Google is replaced and
every stamp cleared, so the replacement fills up on the same run.

read -> plan -> execute: the database side is read into plain LocalEvent
rows, plan_ops (pure) decides the inserts, patches and deletes, orphans
(pure) decides the sweep, and the executor performs each op and stamps its
row in a transaction of its own.

Exit status: 1 when a push failed or the sharing has a problem this system
will not fix on its own (somebody else may write) — the scheduler mails
that once a day. Hand-added entries alone are a warning, not a failure.

Usage: python -m volunteerdb.jobs.calendar_sync
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import sqlalchemy as sa
import structlog

from .. import env as env_mod
from ..db import transaction
from ..env import Env
from ..errors import External
from ..fp import Err, Ok, Result
from ..log import init_logging
from ..models import Event, EventStatus
from ..services import gcal, google_api
from . import job_lock

logger = structlog.get_logger(__name__)


# --- the plan, pure -----------------------------------------------------------


@dataclass(frozen=True)
class LocalEvent:
    """One event as the reconcile sees it: its row's stamps and the payload
    the calendar should show."""

    id: int
    scheduled: bool
    google_id: str | None
    fingerprint: str | None
    payload: dict


@dataclass(frozen=True)
class Insert:
    event_id: int
    payload: dict


@dataclass(frozen=True)
class Patch:
    event_id: int
    google_id: str
    payload: dict


@dataclass(frozen=True)
class Delete:
    event_id: int
    google_id: str


type CalendarOp = Insert | Patch | Delete


def plan_ops(local: Sequence[LocalEvent]) -> list[CalendarOp]:
    """What the calendar needs: an insert for a scheduled event never pushed,
    a patch when its payload drifted, a delete for a cancelled one that is
    still up. An unchanged event costs no API call."""
    ops: list[CalendarOp] = []
    for event in local:
        if not event.scheduled:
            if event.google_id:
                ops.append(Delete(event.id, event.google_id))
            continue
        if event.google_id is None:
            ops.append(Insert(event.id, event.payload))
        elif event.fingerprint != gcal.fingerprint(event.payload):
            ops.append(Patch(event.id, event.google_id, event.payload))
    return ops


def live_ids(local: Sequence[LocalEvent]) -> set[str]:
    return {str(event.id) for event in local if event.scheduled}


def orphans(remote: Sequence[dict], live: set[str]) -> list[str]:
    """Managed calendar entries with no live event behind them (a team
    CASCADE, or a cancellation the run before could not push): their ids."""
    return [
        item["id"]
        for item in remote
        if item.get("id")
        and item.get("extendedProperties", {}).get("private", {}).get("vdb_id")
        not in live
    ]


# --- the executor -------------------------------------------------------------


async def _stamp(
    env: Env, event_id: int, google_event_id: str | None, fp: str | None
) -> None:
    async with transaction(env, None) as session:
        await session.execute(
            sa.update(Event)
            .where(Event.id == event_id)
            .values(google_event_id=google_event_id, google_fingerprint=fp)
        )


async def _ensure_calendar(
    env: Env, client: httpx.AsyncClient, token: str
) -> Result[str, External]:
    """The remembered calendar, replaced when Google no longer has it."""
    async with transaction(env, None) as session:
        stored = await gcal.stored_calendar(session)
    calendar_id = stored["calendar_id"] if stored else None
    if calendar_id is not None:
        exists = await gcal.calendar_exists(client, token, calendar_id)
        if isinstance(exists, Err):
            return exists
        if not exists.value:
            async with transaction(env, None) as session:
                cleared = await gcal.forget_pushes(session)
            logger.warning(
                "calendar_sync.calendar_missing",
                calendar_id=calendar_id,
                cleared=cleared,
            )
            calendar_id = None
    if calendar_id is None:
        name = gcal.calendar_name(env.settings.org_name)
        made = await gcal.create_calendar(
            client, token, name=name, tz=env.settings.timezone
        )
        if isinstance(made, Err):
            return made
        calendar_id = made.value
        async with transaction(env, None) as session:
            await gcal.remember(session, calendar_id, now=env.clock.now(), created=True)
        logger.info(
            "calendar_sync.calendar_created", calendar_id=calendar_id, name=name
        )
    return Ok(calendar_id)


async def read(env: Env, *, horizon: datetime) -> list[LocalEvent]:
    """Everything upcoming-ish, plus anything ever pushed (so a cancellation
    or an old stamp still gets its calendar-side cleanup), as plain values:
    the network calls happen outside any transaction."""
    tz = env.settings.timezone
    async with transaction(env, None) as session:
        rows = await session.scalars(
            sa.select(Event)
            .where(sa.or_(Event.ends_at > horizon, Event.google_event_id.is_not(None)))
            .order_by(Event.starts_at, Event.id)
        )
        return [
            LocalEvent(
                id=e.id,
                scheduled=e.status == EventStatus.scheduled.value,
                google_id=e.google_event_id,
                fingerprint=e.google_fingerprint,
                payload=gcal.event_payload(e, tz),
            )
            for e in rows
        ]


async def _apply(
    env: Env, client: httpx.AsyncClient, token: str, calendar_id: str, op: CalendarOp
) -> Result[None, External]:
    """One op on the calendar, then its row stamped."""
    match op:
        case Delete(event_id, google_id):
            gone = await gcal.delete(client, token, calendar_id, google_id)
            if isinstance(gone, Err):
                return gone
            await _stamp(env, event_id, None, None)
        case Insert(event_id, payload):
            inserted = await gcal.insert(client, token, calendar_id, payload)
            if isinstance(inserted, Err):
                return inserted
            await _stamp(env, event_id, inserted.value, gcal.fingerprint(payload))
        case Patch(event_id, google_id, payload):
            patched = await gcal.patch(client, token, calendar_id, google_id, payload)
            if isinstance(patched, Err):
                return patched
            await _stamp(env, event_id, google_id, gcal.fingerprint(payload))
    return Ok(None)


async def main(env: Env) -> int:
    init_logging()
    cfg = env.google()
    if not gcal.enabled(cfg):
        print("calendar sync: not configured (VDB_SHEETS_* unset)")
        return 0
    async with env.http.client(timeout=gcal.TIMEOUT) as client:
        return await _run(env, cfg, client)


async def _run(
    env: Env, cfg: google_api.GoogleConfig, client: httpx.AsyncClient
) -> int:
    minted = await gcal.mint_token(client, cfg)
    if isinstance(minted, Err):
        logger.error("calendar_sync.token_failed", error=minted.error.message)
        return 1
    token = minted.value
    ensured = await _ensure_calendar(env, client, token)
    if isinstance(ensured, Err):
        logger.error("calendar_sync.calendar_failed", error=ensured.error.message)
        return 1
    calendar_id = ensured.value
    verified = await gcal.verify_readonly(client, token, calendar_id)
    if isinstance(verified, Err):
        logger.error("calendar_sync.calendar_failed", error=verified.error.message)
        return 1
    problems = verified.value
    for problem in problems:
        logger.warning(
            "calendar_sync.acl_problem", calendar_id=calendar_id, problem=problem
        )
    if not problems:
        async with transaction(env, None) as session:
            await gcal.remember(
                session, calendar_id, now=env.clock.now(), verified=True
            )

    horizon = env.clock.now() - timedelta(days=1)
    local = await read(env, horizon=horizon)
    pushed = removed = failed = 0
    for op in plan_ops(local):
        done = await _apply(env, client, token, calendar_id, op)
        if isinstance(done, Err):
            failed += 1
            logger.warning(
                "calendar_sync.push_failed",
                event_id=op.event_id,
                error=done.error.message,
            )
        elif isinstance(op, Delete):
            removed += 1
        else:
            pushed += 1

    # orphan GC: managed calendar entries whose VDB event is gone (or no
    # longer scheduled). Past entries fall outside timeMin and are left as
    # calendar history; hand-created entries lack the marker entirely.
    listed = await gcal.list_managed(client, token, calendar_id, horizon)
    if isinstance(listed, Err):
        failed += 1
        logger.warning("calendar_sync.gc_failed", error=listed.error.message)
    else:
        for google_id in orphans(listed.value, live_ids(local)):
            gone = await gcal.delete(client, token, calendar_id, google_id)
            if isinstance(gone, Err):  # the rest are retried next run
                failed += 1
                logger.warning("calendar_sync.gc_failed", error=gone.error.message)
                break
            removed += 1

    # hand-added entries: the calendar is meant to hold only what this system
    # put there, so each one is named in the log — and left where it is
    unmanaged = 0
    strangers = await gcal.list_unmanaged(client, token, calendar_id, horizon)
    if isinstance(strangers, Err):
        failed += 1
        logger.warning(
            "calendar_sync.unmanaged_scan_failed", error=strangers.error.message
        )
    else:
        for item in strangers.value:
            unmanaged += 1
            logger.warning(
                "calendar_sync.unmanaged_entry",
                google_event_id=item.get("id"),
                summary=item.get("summary"),
            )

    acl = "ACL ok" if not problems else f"{len(problems)} ACL problem(s)"
    print(
        f"calendar sync: {pushed} pushed, {removed} removed, {failed} failure(s), "
        f"{unmanaged} unmanaged, {acl}"
    )
    return 1 if failed or problems else 0


def cli(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    async def locked() -> int:
        env = env_mod.build()
        async with job_lock(env, "calendar_sync") as acquired:
            if not acquired:
                print("skipped: another calendar_sync run holds the job lock")
                return 0
            return await main(env)

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
