"""Nightly event digest (in-app scheduler, VDB_EVENT_REMINDERS_AT):
scheduling notices and reminders.

Exactly two notices exist, batched into ONE email per person per night no
matter how many events it covers: (a) a manager scheduled you for an event
("you have been scheduled"), (b) an event you serve at starts within the
next settings().event_reminder_days parish days ("coming up soon"). Self
sign-ups and substitution claims never get notice (a) — their
assigned_notified_at was stamped at insert, because the person acted
themselves. An event listed as newly scheduled that is already inside the
reminder window is listed once and stamps BOTH columns, so the next night
does not repeat it as a reminder.

The per-assignment stamps (event_assignment.assigned_notified_at /
reminder_sent_at) make each notice one-shot and per-person idempotent: a
failed send leaves its stamps NULL and retries the next night; a crash
mid-run re-sends at most the unstamped people. Windows use the event's
parish-day date (settings().timezone), never the container's UTC clock.

Usage: python -m volunteerdb.jobs.event_reminders [--today YYYY-MM-DD]
(--today exists for manual runs and tests; defaults to the parish's today)
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from ..config import settings
from ..db import db_session
from ..log import init_logging
from ..models import Event, EventAssignment, EventSlot, EventStatus, Volunteer
from ..services import elections, mail
from ..services import teams as team_service
from . import job_lock


async def main(today: date | None = None) -> int:
    init_logging()
    if today is None:
        today = elections.local_today()
    window_end = today + timedelta(days=settings().event_reminder_days)
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
                        EventAssignment.reminder_sent_at.is_(None),
                    ),
                )
                .order_by(Event.starts_at, Event.id)
            )
        ).all()
        paths = team_service.team_paths(await team_service.list_all(session))

    # volunteer id -> (email, digest items, ids to stamp per notice)
    per_person: dict[
        int, tuple[str, list[mail.EventDigestItem], list[int], list[int]]
    ] = {}
    for assignment, event, slot, volunteer in rows:
        event_day = event.starts_at.astimezone(tz).date()
        in_window = today <= event_day <= window_end
        pending_scheduled = assignment.assigned_notified_at is None
        pending_reminder = assignment.reminder_sent_at is None and in_window
        if not pending_scheduled and not pending_reminder:
            continue  # already told; the reminder window is not here yet
        entry = per_person.setdefault(volunteer.id, (volunteer.email, [], [], []))
        entry[1].append(
            mail.EventDigestItem(
                # a newly scheduled event already inside the window is listed
                # once, under the stronger notice
                kind="scheduled" if pending_scheduled else "reminder",
                title=event.title,
                path=paths.get(event.team_id, f"team {event.team_id}"),
                slot=slot.name,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                location=event.location,
            )
        )
        if pending_scheduled:
            entry[2].append(assignment.id)
            if in_window:  # both stamps: never re-listed as a reminder
                entry[3].append(assignment.id)
        else:
            entry[3].append(assignment.id)

    base = settings().public_base_url.rstrip("/")
    events_url = f"{base}/events" if base else None

    sent = failed = 0
    for email, items, scheduled_ids, reminder_ids in per_person.values():
        if not await mail.send_email(
            email, *mail.event_digest_email(items, events_url)
        ):
            failed += 1
            print(f"FAILED digest to {email}", file=sys.stderr)
            continue  # stamps stay NULL — retried the next night
        async with db_session() as session:
            if scheduled_ids:
                await session.execute(
                    sa.update(EventAssignment)
                    .where(EventAssignment.id.in_(scheduled_ids))
                    .values(assigned_notified_at=sa.func.now())
                )
            if reminder_ids:
                await session.execute(
                    sa.update(EventAssignment)
                    .where(EventAssignment.id.in_(reminder_ids))
                    .values(reminder_sent_at=sa.func.now())
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
