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
settings.alert_email if set, and retry after RETRY_DELAY up to
MAX_ATTEMPTS_PER_DAY attempts per parish day. The mail is once per job per
parish day even though the retries are not: three identical alerts for one
broken job is not three times the information, and the mail allowance this
instance runs on is 1,000 a month (services/mail_quota.py). The attempt
counter is in-memory on purpose: a restart resets it, and a redeploy is
often the fix.
Jobs are per-person idempotent (NULL-stamp pattern), so retries and even
double runs are safe. Each run holds the jobs.job_lock advisory lock, so a
manually launched one-shot job container can never overlap the scheduler.

Some jobs run on an *interval* instead of at a nightly time (Job.every):
due whenever the interval has passed since the last attempt — attempt, not
success, so a failing job retries at its own cadence rather than
hot-looping, and MAX_ATTEMPTS_PER_DAY does not apply. last_attempt_at is
hydrated from job_run at startup so a crash-looping container cannot hammer
an external API. (The once-a-day alert rule started here — a 30-minute job
failing all day must not send 48 emails — and now covers the nightly jobs
too.)

The decisions are pure functions over values -- is_due, is_due_every,
due_jobs, and the JobState transitions -- and one Scheduler object, built
by main.run around the Env, owns the loop, the state and the task. Nothing
here is module-global.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from time import perf_counter

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .config import Settings
from .db import transaction
from .env import Env
from .jobs import (
    calendar_sync,
    event_reminders,
    fetch_pages,
    job_lock,
    proposal_digest,
    roster_sync,
    task_force_cleanup,
)
from .models import JobRun

logger = structlog.get_logger(__name__)

TICK_SECONDS = 60
RETRY_DELAY = timedelta(minutes=30)
MAX_ATTEMPTS_PER_DAY = 3
EXIT_EXCEPTION = -1  # sentinel exit code recorded for an uncaught exception


@dataclass(frozen=True)
class Job:
    name: str  # job_run.job_name; also the log/alert label
    at_setting: str | None  # Settings attribute holding the parish-local run time
    run: Callable[[Env], Awaitable[int]]
    # interval mode — exactly one of at_setting/every is set. A literal, not
    # a Setting: a reconcile cadence has no parish-local coupling to tune,
    # and the daily jobs' positional construction stays untouched.
    every: timedelta | None = None


# Registry order is execution order when several jobs are due at once (e.g.
# startup catch-up), preserving the sync -> fetch -> digest -> reminders chain
# the crontab used to space out by half-hours. roster_sync leads: a digest or
# reminder should speak for the roster the sheets just settled on.
JOBS: tuple[Job, ...] = (
    Job("roster_sync", "roster_sync_at", roster_sync.main),
    Job("fetch_pages", "fetch_pages_at", fetch_pages.main),
    Job("proposal_digest", "proposal_digest_at", proposal_digest.main),
    Job("event_reminders", "event_reminders_at", event_reminders.main),
    # last: interval jobs must never delay the nightly chain
    Job("calendar_sync", None, calendar_sync.main, every=timedelta(minutes=30)),
    Job("task_force_cleanup", None, task_force_cleanup.main, every=timedelta(hours=1)),
)


@dataclass(frozen=True)
class JobState:
    """What the scheduler knows about one job: the job_run row's facts plus
    the day's retry bookkeeping. A value -- each transition returns a new one."""

    last_success_on: date | None = None
    attempts_on: date | None = None
    attempts: int = 0
    last_attempt_at: datetime | None = None
    alerted_on: date | None = None  # one failure alert per job per parish day


type SchedulerState = Mapping[str, JobState]


# --- the decisions, pure ------------------------------------------------------


def is_due(scheduled: time, state: JobState, now: datetime) -> bool:
    """Whether a nightly job should run at `now` (parish-local)."""
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
    """Whether an interval job should run at `now`.

    Keyed off the last *attempt*, not success: a failing job retries on its
    own cadence instead of hot-looping every tick, and MAX_ATTEMPTS_PER_DAY
    does not apply — the cadence itself is the limit."""
    return state.last_attempt_at is None or now - state.last_attempt_at >= every


def job_due(job: Job, state: JobState, now: datetime, settings: Settings) -> bool:
    if job.every is not None:
        return is_due_every(job.every, state, now)
    return is_due(getattr(settings, job.at_setting), state, now)


def due_jobs(
    jobs: Sequence[Job], state: SchedulerState, now: datetime, settings: Settings
) -> tuple[Job, ...]:
    """The jobs a tick at `now` runs, in registry order."""
    return tuple(
        job
        for job in jobs
        if job_due(job, state.get(job.name, JobState()), now, settings)
    )


def attempted(state: JobState, now: datetime) -> JobState:
    """The state after a run began at `now`: the day's attempt counter."""
    if state.attempts_on != now.date():
        state = replace(state, attempts_on=now.date(), attempts=0)
    return replace(state, attempts=state.attempts + 1, last_attempt_at=now)


def succeeded(state: JobState, now: datetime) -> JobState:
    return replace(state, last_success_on=now.date())


def alerted(state: JobState, now: datetime) -> JobState:
    return replace(state, alerted_on=now.date())


def alert_message(
    job: Job, exit_code: int, attempt: int, now: datetime
) -> tuple[str, str]:
    """The failure alert's subject and body."""
    when = f"{now:%Y-%m-%d %H:%M %Z}"
    if job.every is None:
        return (
            f"[volunteerdb] nightly {job.name} FAILED",
            f"Job {job.name} failed (exit {exit_code}, attempt "
            f"{attempt}/{MAX_ATTEMPTS_PER_DAY}) at {when}.\n"
            f"It retries after {RETRY_DELAY // timedelta(minutes=1)} minutes, up to "
            f"{MAX_ATTEMPTS_PER_DAY} times a day.\n"
            "This is the only alert for this job today — a further failure will "
            "log, not mail.\n"
            "Details: journalctl -u volunteerdb-app\n",
        )
    return (
        f"[volunteerdb] {job.name} FAILED",
        f"Job {job.name} failed (exit {exit_code}) at {when}.\n"
        f"It runs every {job.every // timedelta(minutes=1)} minutes and keeps "
        "retrying; this alert repeats at most once per day.\n"
        "Details: journalctl -u volunteerdb-app\n",
    )


# --- the loop ---------------------------------------------------------------


class Scheduler:
    """The one loop: owns its task, its state and the Env its jobs run with.
    main.run builds it; tests build their own around a test Env."""

    def __init__(self, env: Env, jobs: Sequence[Job] = JOBS) -> None:
        self.env = env
        self.jobs = tuple(jobs)
        self.state: dict[str, JobState] = {}
        self._task: asyncio.Task | None = None

    def local_now(self) -> datetime:
        return self.env.clock.now().astimezone(self.env.tz)

    async def load_state(self) -> None:
        """job_run, hydrated: the last success, and -- for the interval jobs'
        pacing across restarts -- the last attempt."""
        self.state = {job.name: JobState() for job in self.jobs}
        async with transaction(self.env, None) as session:
            for row in await session.scalars(sa.select(JobRun)):
                if row.job_name in self.state:
                    self.state[row.job_name] = replace(
                        self.state[row.job_name],
                        last_success_on=row.last_success_on,
                        last_attempt_at=row.last_attempt_at,
                    )

    async def record(
        self,
        name: str,
        *,
        exit_code: int,
        success_on: date | None,
        attempted_at: datetime,
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
        async with transaction(self.env, None) as session:
            await session.execute(
                stmt.on_conflict_do_update(index_elements=["job_name"], set_=updates)
            )

    async def run_job(self, job: Job, now: datetime) -> None:
        async with job_lock(self.env, job.name) as acquired:
            if not acquired:
                # a manual `python -m` run is in flight; not an attempt
                logger.warning("scheduler.job_skipped_locked", job=job.name)
                return
            st = attempted(self.state.get(job.name, JobState()), now)
            self.state[job.name] = st
            # audit=True: ranks at AUDIT, so job runs are visible at the
            # production default verbosity (INFO would be filtered out)
            logger.info(
                "scheduler.job_started", job=job.name, attempt=st.attempts, audit=True
            )
            started = perf_counter()
            try:
                code = await job.run(self.env)
            except Exception:
                logger.exception("scheduler.job_crashed", job=job.name)
                code = EXIT_EXCEPTION
        ms = round((perf_counter() - started) * 1000)
        if code == 0:
            self.state[job.name] = succeeded(st, now)
            await self.record(
                job.name, exit_code=0, success_on=now.date(), attempted_at=now
            )
            logger.info("scheduler.job_succeeded", job=job.name, ms=ms, audit=True)
            return
        await self.record(job.name, exit_code=code, success_on=None, attempted_at=now)
        logger.error(
            "scheduler.job_failed",
            job=job.name,
            exit_code=code,
            attempt=st.attempts,
            ms=ms,
        )
        # One alert per job per parish day, whichever kind of job it is. A
        # nightly job retries MAX_ATTEMPTS_PER_DAY times, and alerting on each
        # sent three identical messages for one broken job — a week of that is
        # 21, out of a 1,000-a-month allowance, spent saying the same thing.
        # The retries still happen and still log; only the mail is deduplicated.
        if st.alerted_on != now.date():
            self.state[job.name] = alerted(st, now)
            to = self.env.settings.alert_email
            if to:
                subject, body = alert_message(job, code, st.attempts, now)
                await self.env.mailer.send(to, subject, body)  # never raises

    async def tick(self, now: datetime) -> None:
        for job in due_jobs(self.jobs, self.state, now, self.env.settings):
            await self.run_job(job, now)  # sequential: app jobs never overlap

    async def _loop(self) -> None:
        loaded = False
        while True:
            try:
                if not loaded:  # retried each tick until the DB answers
                    await self.load_state()
                    loaded = True
                    logger.info(
                        "scheduler.started",
                        jobs=[j.name for j in self.jobs],
                        timezone=self.env.settings.timezone,
                        audit=True,
                    )
                await self.tick(self.local_now())
            except Exception:  # a broken tick must never kill the loop
                logger.exception("scheduler.tick_failed")
            await asyncio.sleep(TICK_SECONDS)

    def start(self) -> None:
        """Idempotent; registered as a NiceGUI startup hook (main.run)."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="vdb-scheduler"
            )

    @property
    def running(self) -> bool:
        return self._task is not None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
