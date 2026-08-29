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

Exit status: 1 when a push failed or the sharing has a problem this system
will not fix on its own (somebody else may write) — the scheduler mails
that once a day. Hand-added entries alone are a warning, not a failure.

Usage: python -m volunteerdb.jobs.calendar_sync
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog

from .. import env as env_mod
from ..db import db_session
from ..env import Env
from ..log import init_logging
from ..models import Event, EventStatus
from ..services import gcal
from . import job_lock

logger = structlog.get_logger(__name__)


async def _stamp(event_id: int, google_event_id: str | None, fp: str | None) -> None:
    async with db_session() as session:
        await session.execute(
            sa.update(Event)
            .where(Event.id == event_id)
            .values(google_event_id=google_event_id, google_fingerprint=fp)
        )


async def _ensure_calendar(token: str) -> str:
    """The remembered calendar, replaced when Google no longer has it."""
    async with db_session() as session:
        stored = await gcal.stored_calendar(session)
    calendar_id = stored["calendar_id"] if stored else None
    if calendar_id is not None and not await gcal.calendar_exists(token, calendar_id):
        async with db_session() as session:
            cleared = await gcal.forget_pushes(session)
        logger.warning(
            "calendar_sync.calendar_missing", calendar_id=calendar_id, cleared=cleared
        )
        calendar_id = None
    if calendar_id is None:
        calendar_id = await gcal.create_calendar(token)
        async with db_session() as session:
            await gcal.remember(session, calendar_id, created=True)
        logger.info(
            "calendar_sync.calendar_created",
            calendar_id=calendar_id,
            name=gcal.calendar_name(),
        )
    return calendar_id


async def main(env: Env) -> int:
    init_logging()
    if not gcal.enabled():
        print("calendar sync: not configured (VDB_SHEETS_* unset)")
        return 0
    try:
        token = await gcal.mint_token()
    except gcal.GcalError as exc:
        logger.error("calendar_sync.token_failed", error=str(exc))
        return 1
    try:
        calendar_id = await _ensure_calendar(token)
        problems = await gcal.verify_readonly(token, calendar_id)
    except gcal.GcalError as exc:
        logger.error("calendar_sync.calendar_failed", error=str(exc))
        return 1
    for problem in problems:
        logger.warning(
            "calendar_sync.acl_problem", calendar_id=calendar_id, problem=problem
        )
    if not problems:
        async with db_session() as session:
            await gcal.remember(session, calendar_id, verified=True)

    # everything upcoming-ish, plus anything ever pushed (so a cancellation
    # or an old stamp still gets its calendar-side cleanup)
    horizon = datetime.now(UTC) - timedelta(days=1)
    async with db_session() as session:
        rows = list(
            await session.scalars(
                sa.select(Event)
                .where(
                    sa.or_(
                        Event.ends_at > horizon,
                        Event.google_event_id.is_not(None),
                    )
                )
                .order_by(Event.starts_at, Event.id)
            )
        )
        # network calls happen outside any transaction: snapshot plain values
        snapshots = [
            (
                e.id,
                e.status,
                e.google_event_id,
                e.google_fingerprint,
                gcal.event_payload(e),
            )
            for e in rows
        ]

    pushed = removed = failed = 0
    live_vdb_ids: set[str] = set()
    for event_id, status, google_id, stored_fp, payload in snapshots:
        scheduled = status == EventStatus.scheduled.value
        if scheduled:
            live_vdb_ids.add(str(event_id))
        try:
            if not scheduled:
                if google_id:
                    await gcal.delete(token, calendar_id, google_id)
                    await _stamp(event_id, None, None)
                    removed += 1
                continue
            fp = gcal.fingerprint(payload)
            if google_id is None:
                new_id = await gcal.insert(token, calendar_id, payload)
                await _stamp(event_id, new_id, fp)
                pushed += 1
            elif stored_fp != fp:
                await gcal.patch(token, calendar_id, google_id, payload)
                await _stamp(event_id, google_id, fp)
                pushed += 1
        except gcal.GcalError as exc:
            failed += 1
            logger.warning(
                "calendar_sync.push_failed", event_id=event_id, error=str(exc)
            )

    # orphan GC: managed calendar entries whose VDB event is gone (or no
    # longer scheduled). Past entries fall outside timeMin and are left as
    # calendar history; hand-created entries lack the marker entirely.
    try:
        for item in await gcal.list_managed(token, calendar_id, horizon):
            vdb_id = item.get("extendedProperties", {}).get("private", {}).get("vdb_id")
            if vdb_id in live_vdb_ids or not item.get("id"):
                continue
            await gcal.delete(token, calendar_id, item["id"])
            removed += 1
    except gcal.GcalError as exc:
        failed += 1
        logger.warning("calendar_sync.gc_failed", error=str(exc))

    # hand-added entries: the calendar is meant to hold only what this system
    # put there, so each one is named in the log — and left where it is
    unmanaged = 0
    try:
        for item in await gcal.list_unmanaged(token, calendar_id, horizon):
            unmanaged += 1
            logger.warning(
                "calendar_sync.unmanaged_entry",
                google_event_id=item.get("id"),
                summary=item.get("summary"),
            )
    except gcal.GcalError as exc:
        failed += 1
        logger.warning("calendar_sync.unmanaged_scan_failed", error=str(exc))

    acl = "ACL ok" if not problems else f"{len(problems)} ACL problem(s)"
    print(
        f"calendar sync: {pushed} pushed, {removed} removed, {failed} failure(s), "
        f"{unmanaged} unmanaged, {acl}"
    )
    return 1 if failed or problems else 0


def cli(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    async def locked() -> int:
        async with job_lock("calendar_sync") as acquired:
            if not acquired:
                print("skipped: another calendar_sync run holds the job lock")
                return 0
            return await main(env_mod.build())

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
