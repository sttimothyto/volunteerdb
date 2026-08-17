"""Nightly event digest (in-app scheduler, VDB_EVENT_REMINDERS_AT):
scheduling notices and the two staged reminders.

Three notices exist, batched into ONE email per person per night no matter
how many events it covers: (a) a manager scheduled you for an event ("you
have been scheduled"), (b) an event you serve at is within 7 parish days
("coming up this week"), (c) it starts tomorrow. The week and day stages
are per-assignment PREFERENCES chosen at sign-up (notify_7d / notify_24h,
both pre-checked); an opted-out stage simply never fires. Self sign-ups
and substitution claims never get notice (a) — their assigned_notified_at
was stamped at insert, because the person acted themselves. A row pending
several notices at once is listed once, under the strongest, and stamps
every stage whose window it satisfied, so the next night cannot repeat it
under a weaker one.

The per-assignment stamps (assigned_notified_at / reminded_7d_at /
reminded_24h_at) make each notice one-shot and per-person idempotent: a
failed send leaves its stamps NULL and retries the next night; a crash
mid-run re-sends at most the unstamped people. Windows use the event's
parish-day date (settings().timezone), never the container's UTC clock —
"tomorrow" means the digest sent the morning of the day before.

Usage: python -m volunteerdb.jobs.event_reminders [--today YYYY-MM-DD]
(--today exists for manual runs and tests; defaults to the parish's today)
"""

import argparse
import asyncio
import sys
from datetime import date
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from ..config import settings
from ..db import db_session
from ..log import init_logging
from ..models import Event, EventAssignment, EventSlot, EventStatus, Volunteer
from ..services import elections, mail
from ..services import teams as team_service
from . import job_lock

WEEK_DAYS = 7
DAY_DAYS = 1


async def main(today: date | None = None) -> int:
    init_logging()
    if today is None:
        today = elections.local_today()
    tz = ZoneInfo(settings().timezone)

    async with db_session() as session:
        rows = (
            await session.execute(
                sa.select(EventAssignment, Event, EventSlot, Volunteer)
                .join(Event, Event.id == EventAssignment.event_id)
                .join(EventSlot, EventSlot.id == EventAssignment.slot_id)
                .join(Volunteer, Volunteer.id == EventAssignment.volunteer_id)
                .where(
                    Event.status == EventStatus.scheduled.value,
                    Event.ends_at > sa.func.now(),
                    Volunteer.email.is_not(None),
                    sa.or_(
                        EventAssignment.assigned_notified_at.is_(None),
                        sa.and_(
                            EventAssignment.notify_7d,
                            EventAssignment.reminded_7d_at.is_(None),
                        ),
                        sa.and_(
                            EventAssignment.notify_24h,
                            EventAssignment.reminded_24h_at.is_(None),
                        ),
                    ),
                )
                .order_by(Event.starts_at, Event.id)
            )
        ).all()
        paths = team_service.team_paths(await team_service.list_all(session))

    # volunteer id -> (email, digest items, ids to stamp per stage)
    per_person: dict[
        int, tuple[str, list[mail.EventDigestItem], list[int], list[int], list[int]]
    ] = {}
    for assignment, event, slot, volunteer in rows:
        event_day = event.starts_at.astimezone(tz).date()
        days_out = (event_day - today).days
        pending_scheduled = assignment.assigned_notified_at is None
        pending_week = (
            assignment.notify_7d
            and assignment.reminded_7d_at is None
            and days_out <= WEEK_DAYS
        )
        pending_day = (
            assignment.notify_24h
            and assignment.reminded_24h_at is None
            and days_out <= DAY_DAYS
        )
        if not (pending_scheduled or pending_week or pending_day):
            continue  # already told; no reminder window is here yet
        entry = per_person.setdefault(volunteer.id, (volunteer.email, [], [], [], []))
        # listed once, under the strongest notice
        kind = "scheduled" if pending_scheduled else "day" if pending_day else "week"
        entry[1].append(
            mail.EventDigestItem(
                kind=kind,
                title=event.title,
                path=paths.get(event.team_id, f"team {event.team_id}"),
                slot=slot.name,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                location=event.location,
            )
        )
        # every stage whose window is satisfied stamps now — the event was
        # communicated tonight, whatever heading it sat under
        if pending_scheduled:
            entry[2].append(assignment.id)
        if assignment.reminded_7d_at is None and days_out <= WEEK_DAYS:
            entry[3].append(assignment.id)
        if assignment.reminded_24h_at is None and days_out <= DAY_DAYS:
            entry[4].append(assignment.id)

    base = settings().public_base_url.rstrip("/")
    events_url = f"{base}/events" if base else None

    sent = failed = 0
    for email, items, scheduled_ids, week_ids, day_ids in per_person.values():
        if not await mail.send_email(
            email, *mail.event_digest_email(items, events_url)
        ):
            failed += 1
            print(f"FAILED digest to {email}", file=sys.stderr)
            continue  # stamps stay NULL — retried the next night
        async with db_session() as session:
            for ids, column in (
                (scheduled_ids, "assigned_notified_at"),
                (week_ids, "reminded_7d_at"),
                (day_ids, "reminded_24h_at"),
            ):
                if ids:
                    await session.execute(
                        sa.update(EventAssignment)
                        .where(EventAssignment.id.in_(ids))
                        .values(**{column: sa.func.now()})
                    )
        sent += 1

    print(f"event reminders: {sent} person(s) emailed, {failed} failure(s)")
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="pretend today is this date (parish day); for manual runs and tests",
    )
    args = parser.parse_args(argv)

    async def locked() -> int:
        async with job_lock("event_reminders") as acquired:
            if not acquired:
                print("skipped: another event_reminders run holds the job lock")
                return 0
            return await main(today=args.today)

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
