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
from datetime import timedelta

import httpx
import sqlalchemy as sa
import structlog

from .. import env as env_mod
from ..db import db_session
from ..env import Env
from ..errors import External
from ..fp import Err, Ok, Result
from ..log import init_logging
from ..models import Event, EventStatus
from ..services import gcal, google_api
from . import job_lock

logger = structlog.get_logger(__name__)


async def _stamp(event_id: int, google_event_id: str | None, fp: str | None) -> None:
    async with db_session() as session:
        await session.execute(
            sa.update(Event)
            .where(Event.id == event_id)
            .values(google_event_id=google_event_id, google_fingerprint=fp)
        )


async def _ensure_calendar(
    env: Env, client: httpx.AsyncClient, token: str
) -> Result[str, External]:
    """The remembered calendar, replaced when Google no longer has it."""
    async with db_session() as session:
        stored = await gcal.stored_calendar(session)
    calendar_id = stored["calendar_id"] if stored else None
    if calendar_id is not None:
        exists = await gcal.calendar_exists(client, token, calendar_id)
        if isinstance(exists, Err):
            return exists
        if not exists.value:
            async with db_session() as session:
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
        async with db_session() as session:
            await gcal.remember(session, calendar_id, now=env.clock.now(), created=True)
        logger.info(
            "calendar_sync.calendar_created", calendar_id=calendar_id, name=name
        )
    return Ok(calendar_id)


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
        async with db_session() as session:
            await gcal.remember(
                session, calendar_id, now=env.clock.now(), verified=True
            )

    # everything upcoming-ish, plus anything ever pushed (so a cancellation
    # or an old stamp still gets its calendar-side cleanup)
    horizon = env.clock.now() - timedelta(days=1)
    tz = env.settings.timezone
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
                gcal.event_payload(e, tz),
            )
            for e in rows
        ]

    pushed = removed = failed = 0
    live_vdb_ids: set[str] = set()
    for event_id, status, google_id, stored_fp, payload in snapshots:
        scheduled = status == EventStatus.scheduled.value
        if scheduled:
            live_vdb_ids.add(str(event_id))
        pushed_now, removed_now, error = await _converge(
            client,
            token,
            calendar_id,
            event_id,
            scheduled,
            google_id,
            stored_fp,
            payload,
        )
        pushed += pushed_now
        removed += removed_now
        if error is not None:
            failed += 1
            logger.warning(
                "calendar_sync.push_failed", event_id=event_id, error=error.message
            )

    # orphan GC: managed calendar entries whose VDB event is gone (or no
    # longer scheduled). Past entries fall outside timeMin and are left as
    # calendar history; hand-created entries lack the marker entirely.
    swept = await _collect_orphans(client, token, calendar_id, horizon, live_vdb_ids)
    if isinstance(swept, Err):
        failed += 1
        logger.warning("calendar_sync.gc_failed", error=swept.error.message)
    else:
        removed += swept.value

    # hand-added entries: the calendar is meant to hold only what this system
    # put there, so each one is named in the log — and left where it is
    unmanaged = 0
    listed = await gcal.list_unmanaged(client, token, calendar_id, horizon)
    if isinstance(listed, Err):
        failed += 1
        logger.warning(
            "calendar_sync.unmanaged_scan_failed", error=listed.error.message
        )
    else:
        for item in listed.value:
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


async def _converge(
    client: httpx.AsyncClient,
    token: str,
    calendar_id: str,
    event_id: int,
    scheduled: bool,
    google_id: str | None,
    stored_fp: str | None,
    payload: dict,
) -> tuple[int, int, External | None]:
    """One event onto the calendar: (pushed, removed, the failure if any).
    Each converged event stamps its own row, so a mid-run crash re-converges
    next run."""
    if not scheduled:
        if not google_id:
            return 0, 0, None
        gone = await gcal.delete(client, token, calendar_id, google_id)
        if isinstance(gone, Err):
            return 0, 0, gone.error
        await _stamp(event_id, None, None)
        return 0, 1, None
    fp = gcal.fingerprint(payload)
    if google_id is None:
        inserted = await gcal.insert(client, token, calendar_id, payload)
        if isinstance(inserted, Err):
            return 0, 0, inserted.error
        await _stamp(event_id, inserted.value, fp)
        return 1, 0, None
    if stored_fp != fp:
        patched = await gcal.patch(client, token, calendar_id, google_id, payload)
        if isinstance(patched, Err):
            return 0, 0, patched.error
        await _stamp(event_id, google_id, fp)
        return 1, 0, None
    return 0, 0, None


async def _collect_orphans(
    client: httpx.AsyncClient,
    token: str,
    calendar_id: str,
    horizon,
    live_vdb_ids: set[str],
) -> Result[int, External]:
    """Delete managed entries with no live event behind them; how many went.
    The first failure ends the sweep — the rest are retried next run."""
    listed = await gcal.list_managed(client, token, calendar_id, horizon)
    if isinstance(listed, Err):
        return listed
    removed = 0
    for item in listed.value:
        vdb_id = item.get("extendedProperties", {}).get("private", {}).get("vdb_id")
        if vdb_id in live_vdb_ids or not item.get("id"):
            continue
        gone = await gcal.delete(client, token, calendar_id, item["id"])
        if isinstance(gone, Err):
            return gone
        removed += 1
    return Ok(removed)


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
