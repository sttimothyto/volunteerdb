"""Nightly event digest (in-app scheduler, VDB_EVENT_REMINDERS_AT):
scheduling notices and the two staged reminders.

Three notices exist, batched into ONE email per person per night no matter
how many events it covers: (a) a manager scheduled you for an event ("you
have been scheduled"), (b) an event you serve at is within 7 parish days
("coming up this week"), (c) it starts tomorrow. The week and day stages
are per-assignment PREFERENCES chosen at sign-up (notify_7d / notify_24h);
an opted-out stage simply never fires. Only the 24-hour one is on by
default: the week notice restates the scheduling notice a volunteer already
had, and on the 200-message/day mail allowance that made it a third of all
event mail for nothing (services/mail_quota.py). Self sign-ups
and substitution claims never get notice (a) — that stage is recorded as
already sent when the row is created, because the person acted themselves.
A row pending several notices at once is listed once, under the strongest,
and records every stage whose window it satisfied, so the next night cannot
repeat it under a weaker one.

Rows in `notification`, keyed by (assignment, stage), make each notice
one-shot and per-person idempotent: a failed send writes nothing and retries
the next night; a crash mid-run re-sends at most the people with no row yet.
(These used to be three columns on the assignment itself, so a fourth notice
would have meant a fourth column.) Windows use the event's
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
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .. import env as env_mod
from ..config import settings
from ..db import db_session
from ..env import Env
from ..log import init_logging
from ..models import (
    Event,
    EventAssignment,
    EventSlot,
    EventStatus,
    Notification,
    NotificationStage,
    Volunteer,
)
from ..services import elections, mail
from ..services import teams as team_service
from . import job_lock

WEEK_DAYS = 7
DAY_DAYS = 1


def _unsent(stage: NotificationStage):
    """ "this assignment has had no `stage` notice yet", as a NOT EXISTS.

    The uq_notification_assignment unique on (assignment_id, stage) is what
    makes each of these an index lookup rather than a scan."""
    return ~sa.exists().where(
        Notification.assignment_id == EventAssignment.id,
        Notification.stage == stage,
    )


async def main(env: Env, today: date | None = None) -> int:
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
                    Event.status == EventStatus.scheduled,
                    Event.ends_at > sa.func.now(),
                    Volunteer.email.is_not(None),
                    # anything still owed one of the three: an anti-join per
                    # stage, where it used to be an OR over three columns
                    sa.or_(
                        _unsent(NotificationStage.event_scheduled),
                        sa.and_(
                            EventAssignment.notify_7d,
                            _unsent(NotificationStage.event_week),
                        ),
                        sa.and_(
                            EventAssignment.notify_24h,
                            _unsent(NotificationStage.event_day),
                        ),
                    ),
                )
                .order_by(Event.starts_at, Event.id)
            )
        ).all()
        paths = (await team_service.tree(session)).paths
        # which stages these assignments have already had, in one query: the
        # per-row decisions below are then plain set membership
        already = (
            {
                (row.assignment_id, row.stage)
                for row in await session.execute(
                    sa.select(Notification.assignment_id, Notification.stage).where(
                        Notification.assignment_id.in_([a.id for a, _e, _s, _v in rows])
                    )
                )
            }
            if rows
            else set()
        )

    # volunteer id -> (email, digest items, ids to stamp per stage)
    per_person: dict[
        int, tuple[str, list[mail.EventDigestItem], list[int], list[int], list[int]]
    ] = {}
    for assignment, event, slot, volunteer in rows:
        event_day = event.starts_at.astimezone(tz).date()
        days_out = (event_day - today).days

        def sent(stage: NotificationStage, _id: int = assignment.id) -> bool:
            return (_id, stage) in already

        pending_scheduled = not sent(NotificationStage.event_scheduled)
        pending_week = (
            assignment.notify_7d
            and not sent(NotificationStage.event_week)
            and days_out <= WEEK_DAYS
        )
        pending_day = (
            assignment.notify_24h
            and not sent(NotificationStage.event_day)
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
        # every stage whose window is satisfied is recorded now — the event was
        # communicated tonight, whatever heading it sat under
        if pending_scheduled:
            entry[2].append(assignment.id)
        if not sent(NotificationStage.event_week) and days_out <= WEEK_DAYS:
            entry[3].append(assignment.id)
        if not sent(NotificationStage.event_day) and days_out <= DAY_DAYS:
            entry[4].append(assignment.id)

    base = settings().public_base_url.rstrip("/")
    events_url = f"{base}/events" if base else None

    sent_count = failed = 0
    for email, items, scheduled_ids, week_ids, day_ids in per_person.values():
        if not await mail.send_email(
            email, *mail.event_digest_email(items, events_url)
        ):
            failed += 1
            print(f"FAILED digest to {email}", file=sys.stderr)
            continue  # stamps stay NULL — retried the next night
        async with db_session() as session:
            for ids, stage in (
                (scheduled_ids, NotificationStage.event_scheduled),
                (week_ids, NotificationStage.event_week),
                (day_ids, NotificationStage.event_day),
            ):
                if ids:
                    await session.execute(
                        pg_insert(Notification)
                        .values([{"assignment_id": i, "stage": stage} for i in ids])
                        # a stage recorded between the read and here is fine:
                        # the notice went out either way
                        .on_conflict_do_nothing(constraint="uq_notification_assignment")
                    )
        sent_count += 1

    print(f"event reminders: {sent_count} person(s) emailed, {failed} failure(s)")
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
            return await main(env_mod.build(), today=args.today)

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
