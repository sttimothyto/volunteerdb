"""In-app nightly job scheduler (replaced the host crontab for app jobs).

A 60-second tick loop over a registry of (job, parish-local time) pairs. The
due rule is deliberately a single predicate — run a job once its configured
time has passed and it has not succeeded on the parish today — because that
one rule covers every awkward case at once: a restart or redeploy at any
hour cannot skip a night (job_run persists the last success; startup catch-up
falls out of "not succeeded today"), a completed job never re-runs, and a
clock jump in either direction is absorbed on the next tick. Ticking beats
sleep-until-next for the same reason: no wake-time recomputation to get
wrong. DST is a non-issue at these hours — America/Toronto's spring-forward
only removes 02:00-02:59, and even a removed wall time would just fire on
the first tick after the gap.

Failures (nonzero exit or an exception) log at ERROR, email
settings().alert_email if set, and retry after RETRY_DELAY up to
MAX_ATTEMPTS_PER_DAY attempts per parish day. The attempt counter is
in-memory on purpose: a restart resets it, and a redeploy is often the fix.
Jobs are per-person idempotent (NULL-stamp pattern), so retries and even
double runs are safe. Each run holds the jobs.job_lock advisory lock, so a
manually launched one-shot job container can never overlap the scheduler.

Some jobs run on an *interval* instead of at a nightly time (Job.every):
due whenever the interval has passed since the last attempt — attempt, not
success, so a failing job retries at its own cadence rather than
hot-looping, and MAX_ATTEMPTS_PER_DAY does not apply. last_attempt_at is
hydrated from job_run at startup so a crash-looping container cannot hammer
an external API, and a persistent failure alerts at most once per parish
day (a 30-minute job failing all day must not send 48 emails).
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from time import perf_counter
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .config import settings
from .db import db_session
from .jobs import event_reminders, fetch_pages, job_lock, proposal_digest
from .models import JobRun
from .services import mail

logger = structlog.get_logger(__name__)

TICK_SECONDS = 60
RETRY_DELAY = timedelta(minutes=30)
MAX_ATTEMPTS_PER_DAY = 3
EXIT_EXCEPTION = -1  # sentinel exit code recorded for an uncaught exception


@dataclass(frozen=True)
class Job:
    name: str  # job_run.job_name; also the log/alert label
    at_setting: str | None  # Settings attribute holding the parish-local run time
    run: Callable[[], Awaitable[int]]
    # interval mode — exactly one of at_setting/every is set. A literal, not
    # a Setting: a reconcile cadence has no parish-local coupling to tune,
    # and the daily jobs' positional construction stays untouched.
    every: timedelta | None = None


# Registry order is execution order when several jobs are due at once (e.g.
# startup catch-up), preserving the fetch -> digest -> reminders chain the
# crontab used to space out by half-hours.
JOBS: tuple[Job, ...] = (
    Job("fetch_pages", "fetch_pages_at", fetch_pages.main),
    Job("proposal_digest", "proposal_digest_at", proposal_digest.main),
    Job("event_reminders", "event_reminders_at", event_reminders.main),
)


@dataclass
class JobState:
    """In-memory mirror of job_run plus the day's retry bookkeeping."""

    last_success_on: date | None = None
    attempts_on: date | None = None
    attempts: int = 0
    last_attempt_at: datetime | None = None
    alerted_on: date | None = None  # interval jobs: one failure alert per day


_task: asyncio.Task | None = None
_state: dict[str, JobState] = {}


def local_now() -> datetime:
    return datetime.now(ZoneInfo(settings().timezone))


def is_due(scheduled: time, state: JobState, now: datetime) -> bool:
    """Whether a job should run at `now` (parish-local). Pure — unit-tested."""
    today = now.date()
    # >= not ==: a backwards clock jump must not re-run a finished night
    if state.last_success_on is not None and state.last_success_on >= today:
        return False
    if now.time() < scheduled:
        return False
    if state.attempts_on == today:
        if state.attempts >= MAX_ATTEMPTS_PER_DAY:
            return False
        if (
            state.last_attempt_at is not None
            and now - state.last_attempt_at < RETRY_DELAY
        ):
            return False
    return True


def is_due_every(every: timedelta, state: JobState, now: datetime) -> bool:
    """Whether an interval job should run at `now`. Pure — unit-tested.

    Keyed off the last *attempt*, not success: a failing job retries on its
    own cadence instead of hot-looping every tick, and MAX_ATTEMPTS_PER_DAY
    does not apply — the cadence itself is the limit."""
    return state.last_attempt_at is None or now - state.last_attempt_at >= every


async def _load_state() -> None:
    _state.clear()
    for job in JOBS:
        _state[job.name] = JobState()
    async with db_session() as session:
        for row in await session.scalars(sa.select(JobRun)):
            if row.job_name in _state:
                _state[row.job_name].last_success_on = row.last_success_on
                # interval jobs pace off this even across restarts, so a
                # crash-looping container cannot hammer an external API
                _state[row.job_name].last_attempt_at = row.last_attempt_at


async def _record(
    name: str, *, exit_code: int, success_on: date | None, attempted_at: datetime
) -> None:
    # Core ON CONFLICT upsert: atomic insert-or-update in one statement
    stmt = pg_insert(JobRun).values(
        job_name=name,
        last_success_on=success_on,
        last_attempt_at=attempted_at,
        last_exit_code=exit_code,
    )
    updates = {
        "last_attempt_at": stmt.excluded.last_attempt_at,
        "last_exit_code": stmt.excluded.last_exit_code,
    }
    if success_on is not None:  # a failure must not erase the last success
        updates["last_success_on"] = stmt.excluded.last_success_on
    async with db_session() as session:
        await session.execute(
            stmt.on_conflict_do_update(index_elements=["job_name"], set_=updates)
        )


async def _alert(name: str, exit_code: int, attempt: int) -> None:
    to = settings().alert_email
    if not to:
        return
    await mail.send_email(  # never raises
        to,
        f"[volunteerdb] nightly {name} FAILED",
        f"Job {name} failed (exit {exit_code}, attempt "
        f"{attempt}/{MAX_ATTEMPTS_PER_DAY}) at {local_now():%Y-%m-%d %H:%M %Z}.\n"
        f"It retries after {RETRY_DELAY // timedelta(minutes=1)} minutes.\n"
        "Details: journalctl -u volunteerdb-app\n",
    )


async def _alert_interval(name: str, exit_code: int, every: timedelta) -> None:
    to = settings().alert_email
    if not to:
        return
    await mail.send_email(  # never raises
        to,
        f"[volunteerdb] {name} FAILED",
        f"Job {name} failed (exit {exit_code}) at "
        f"{local_now():%Y-%m-%d %H:%M %Z}.\n"
        f"It runs every {every // timedelta(minutes=1)} minutes and keeps "
        "retrying; this alert repeats at most once per day.\n"
        "Details: journalctl -u volunteerdb-app\n",
    )


async def _run_job(job: Job, now: datetime) -> None:
    st = _state[job.name]
    async with job_lock(job.name) as acquired:
        if not acquired:
            # a manual `python -m` run is in flight; not an attempt
            logger.warning("scheduler.job_skipped_locked", job=job.name)
            return
        if st.attempts_on != now.date():
            st.attempts_on, st.attempts = now.date(), 0
        st.attempts += 1
        st.last_attempt_at = now
        # audit=True: ranks at AUDIT, so job runs are visible at the
        # production default verbosity (INFO would be filtered out)
        logger.info(
            "scheduler.job_started", job=job.name, attempt=st.attempts, audit=True
        )
        started = perf_counter()
        try:
            code = await job.run()
        except Exception:
            logger.exception("scheduler.job_crashed", job=job.name)
            code = EXIT_EXCEPTION
    ms = round((perf_counter() - started) * 1000)
    if code == 0:
        st.last_success_on = now.date()
        await _record(job.name, exit_code=0, success_on=now.date(), attempted_at=now)
        logger.info("scheduler.job_succeeded", job=job.name, ms=ms, audit=True)
    else:
        await _record(job.name, exit_code=code, success_on=None, attempted_at=now)
        logger.error(
            "scheduler.job_failed",
            job=job.name,
            exit_code=code,
            attempt=st.attempts,
            ms=ms,
        )
        if job.every is None:
            await _alert(job.name, code, st.attempts)
        elif st.alerted_on != now.date():
            st.alerted_on = now.date()
            await _alert_interval(job.name, code, job.every)


def _job_due(job: Job, now: datetime) -> bool:
    if job.every is not None:
        return is_due_every(job.every, _state[job.name], now)
    return is_due(getattr(settings(), job.at_setting), _state[job.name], now)


async def _tick(now: datetime) -> None:
    for job in JOBS:  # sequential: app jobs never overlap each other
        if _job_due(job, now):
            await _run_job(job, now)


async def _loop() -> None:
    loaded = False
    while True:
        try:
            if not loaded:  # retried each tick until the DB answers
                await _load_state()
                loaded = True
                logger.info(
                    "scheduler.started",
                    jobs=[j.name for j in JOBS],
                    timezone=settings().timezone,
                    audit=True,
                )
            await _tick(local_now())
        except Exception:  # a broken tick must never kill the loop
            logger.exception("scheduler.tick_failed")
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    """Idempotent; registered as a NiceGUI startup hook (main.run)."""
    global _task
    if _task is None:
        _task = asyncio.get_running_loop().create_task(_loop(), name="vdb-scheduler")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        with suppress(asyncio.CancelledError):
            await _task
        _task = None
