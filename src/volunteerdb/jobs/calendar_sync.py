"""Google Calendar reconcile (in-app scheduler, every 30 minutes).

One-way, VolunteerDB → the public parish calendar. Each run converges the
calendar onto the database: scheduled events are inserted, or patched when
their payload fingerprint drifted; cancelled events are deleted; and a
final sweep deletes managed calendar entries whose VDB event no longer
exists (a team CASCADE is the only path there — no API deletes events).
Nothing pushes from the create/edit handlers on purpose: the sync is the
single writer, and ≤30 minutes from edit to calendar is the documented
deal. Each converged event stamps its own row (google_event_id +
google_fingerprint), so a mid-run crash re-converges next run.

Usage: python -m volunteerdb.jobs.calendar_sync
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog

from ..db import db_session
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


async def main() -> int:
    init_logging()
    if not gcal.enabled():
        print("calendar sync: not configured (VDB_GCAL_* unset)")
        return 0
    try:
        token = await gcal.mint_token()
    except gcal.GcalError as exc:
        logger.error("calendar_sync.token_failed", error=str(exc))
        return 1

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
                    await gcal.delete(token, google_id)
                    await _stamp(event_id, None, None)
                    removed += 1
                continue
            fp = gcal.fingerprint(payload)
            if google_id is None:
                new_id = await gcal.insert(token, payload)
                await _stamp(event_id, new_id, fp)
                pushed += 1
            elif stored_fp != fp:
                await gcal.patch(token, google_id, payload)
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
        for item in await gcal.list_managed(token, horizon):
            vdb_id = item.get("extendedProperties", {}).get("private", {}).get("vdb_id")
            if vdb_id in live_vdb_ids or not item.get("id"):
                continue
            await gcal.delete(token, item["id"])
            removed += 1
    except gcal.GcalError as exc:
        failed += 1
        logger.warning("calendar_sync.gc_failed", error=str(exc))

    print(f"calendar sync: {pushed} pushed, {removed} removed, {failed} failure(s)")
    return 1 if failed else 0


def cli(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    async def locked() -> int:
        async with job_lock("calendar_sync") as acquired:
            if not acquired:
                print("skipped: another calendar_sync run holds the job lock")
                return 0
            return await main()

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
