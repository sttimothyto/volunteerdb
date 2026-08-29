"""The in-app scheduler: the due predicate, restart catch-up, the retry and
alert policy, the advisory job lock, and the create_app seam (tests must
never start real job loops)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from nicegui import app

from volunteerdb import scheduler
from volunteerdb.config import settings
from volunteerdb.jobs import job_lock
from volunteerdb.models import JobRun
from volunteerdb.scheduler import JobState, Scheduler, is_due, is_due_every

from tests.conftest import db_session

TZ = ZoneInfo("America/Toronto")
AT = time(4, 0)
TODAY = date(2026, 8, 16)
YESTERDAY = TODAY - timedelta(days=1)


def _now(day: date, hh: int = 4, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), TZ)


@pytest.fixture
def sched(env) -> Scheduler:
    """A scheduler of its own around the test Env -- nothing is module-global
    -- with the alert address pinned empty regardless of the dev .env."""
    quiet = settings().model_copy(update={"alert_email": ""})
    return Scheduler(env.with_(settings=quiet), jobs=())


def _alerting(sched: Scheduler) -> None:
    """Give the scheduler an alert address."""
    loud = sched.env.settings.model_copy(update={"alert_email": "ops@example.org"})
    sched.env = sched.env.with_(settings=loud)


@pytest.fixture
def sent_mail(env) -> list[tuple[str, str, str]]:
    """What the scheduler mailed: the Env's recording mailer."""
    env.mailer.sent.clear()
    return env.mailer.sent


def _patch_jobs(sched: Scheduler, behaviors: dict[str, int | Exception]) -> list[str]:
    """Give the scheduler stub jobs; returns the call-order log.

    All stubs reuse the real event_reminders_at setting (04:00) — tests pick
    `now` relative to that.
    """
    calls: list[str] = []

    def make(name: str, outcome: int | Exception):
        async def run(env) -> int:
            calls.append(name)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return run

    sched.jobs = tuple(
        scheduler.Job(name, "event_reminders_at", make(name, outcome))
        for name, outcome in behaviors.items()
    )
    return calls


async def _row(name: str) -> JobRun | None:
    async with db_session() as session:
        return await session.get(JobRun, name)


# --- the due predicate (pure) -------------------------------------------------


def test_not_due_before_scheduled_time():
    assert not is_due(AT, JobState(), _now(TODAY, 3, 59))


def test_due_at_and_after_scheduled_time():
    assert is_due(AT, JobState(), _now(TODAY, 4, 0))
    assert is_due(AT, JobState(), _now(TODAY, 23, 59))


def test_not_due_after_success_today():
    assert not is_due(AT, JobState(last_success_on=TODAY), _now(TODAY, 5))


def test_not_due_when_clock_jumped_backwards():
    # last success recorded "in the future" must not re-run the night
    state = JobState(last_success_on=TODAY + timedelta(days=1))
    assert not is_due(AT, state, _now(TODAY, 5))


def test_due_when_last_success_was_yesterday():
    # the restart-catch-up guarantee: a redeploy at 10:00 still runs the night
    assert is_due(AT, JobState(last_success_on=YESTERDAY), _now(TODAY, 10))


def test_retry_waits_out_the_delay():
    state = JobState(attempts_on=TODAY, attempts=1, last_attempt_at=_now(TODAY, 4, 0))
    assert not is_due(AT, state, _now(TODAY, 4, 5))
    assert is_due(AT, state, _now(TODAY, 4, 31))


def test_attempt_budget_is_per_day():
    spent = JobState(
        attempts_on=TODAY,
        attempts=scheduler.MAX_ATTEMPTS_PER_DAY,
        last_attempt_at=_now(TODAY, 5),
    )
    assert not is_due(AT, spent, _now(TODAY, 23))
    stale = JobState(
        attempts_on=YESTERDAY,
        attempts=scheduler.MAX_ATTEMPTS_PER_DAY,
        last_attempt_at=_now(YESTERDAY, 5),
    )
    assert is_due(AT, stale, _now(TODAY, 4, 30))


# --- ticking ------------------------------------------------------------------


async def test_tick_runs_due_jobs_in_registry_order(database, sched):
    calls = _patch_jobs(sched, {"a": 0, "b": 0, "c": 0})
    await sched.load_state()
    await sched.tick(_now(TODAY, 5))
    assert calls == ["a", "b", "c"]
    async with db_session() as session:
        rows = (await session.scalars(sa.select(JobRun))).all()
    assert {r.job_name: (r.last_success_on, r.last_exit_code) for r in rows} == {
        name: (TODAY, 0) for name in "abc"
    }
    # a later tick the same day is a no-op
    await sched.tick(_now(TODAY, 6))
    assert calls == ["a", "b", "c"]


async def test_restart_catches_up_a_missed_night(database, sched):
    calls = _patch_jobs(sched, {"a": 0})
    await sched.record(
        "a", exit_code=0, success_on=YESTERDAY, attempted_at=_now(YESTERDAY)
    )
    await sched.load_state()  # what start() does after a restart
    await sched.tick(_now(TODAY, 10))
    assert calls == ["a"]


async def test_restart_after_success_does_not_rerun(database, sched):
    calls = _patch_jobs(sched, {"a": 0})
    await sched.record("a", exit_code=0, success_on=TODAY, attempted_at=_now(TODAY))
    await sched.load_state()
    await sched.tick(_now(TODAY, 5))
    assert calls == []


# --- failure handling ---------------------------------------------------------


async def test_failure_records_alerts_and_retries(
    database, sent_mail, log_records, sched
):
    _alerting(sched)
    calls = _patch_jobs(sched, {"a": 1})
    await sched.load_state()

    await sched.tick(_now(TODAY, 4, 0))
    assert calls == ["a"]
    row = await _row("a")
    assert (row.last_exit_code, row.last_success_on) == (1, None)
    assert [(to, subject) for to, subject, _ in sent_mail] == [
        ("ops@example.org", "[volunteerdb] nightly a FAILED")
    ]
    assert any(e["event"] == "scheduler.job_failed" for e in log_records)

    await sched.tick(_now(TODAY, 4, 15))  # inside RETRY_DELAY
    assert len(calls) == 1
    await sched.tick(_now(TODAY, 4, 31))
    assert len(calls) == 2
    await sched.tick(_now(TODAY, 5, 2))
    assert len(calls) == 3
    await sched.tick(_now(TODAY, 6, 0))  # budget spent for the day
    assert len(calls) == 3
    assert len(sent_mail) == 1, (
        "three retries, one alert: the second and third say nothing the first "
        "did not, and this instance sends on a 1,000-a-month allowance"
    )

    await sched.tick(_now(TODAY + timedelta(days=1), 4, 0))
    assert len(calls) == 4
    assert len(sent_mail) == 2, "a new parish day alerts afresh"


async def test_failure_without_alert_email_sends_nothing(database, sent_mail, sched):
    _patch_jobs(sched, {"a": 1})
    await sched.load_state()
    await sched.tick(_now(TODAY, 5))
    assert sent_mail == []


async def test_exception_is_a_failure(database, sched):
    calls = _patch_jobs(sched, {"a": RuntimeError("boom")})
    await sched.load_state()
    await sched.tick(_now(TODAY, 4, 0))
    row = await _row("a")
    assert row.last_exit_code == scheduler.EXIT_EXCEPTION
    assert row.last_success_on is None
    await sched.tick(_now(TODAY, 4, 10))  # loop state intact: delay holds
    assert len(calls) == 1


async def test_failure_does_not_erase_last_success(database, sched):
    _patch_jobs(sched, {"a": 1})
    await sched.record(
        "a", exit_code=0, success_on=YESTERDAY, attempted_at=_now(YESTERDAY)
    )
    await sched.load_state()
    await sched.tick(_now(TODAY, 5))
    row = await _row("a")
    assert (row.last_exit_code, row.last_success_on) == (1, YESTERDAY)


# --- interval jobs ------------------------------------------------------------


def _patch_interval_job(
    sched: Scheduler, outcome: int | Exception, every: timedelta = timedelta(minutes=30)
) -> list[str]:
    calls: list[str] = []

    async def run(env) -> int:
        calls.append("i")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sched.jobs = (scheduler.Job("i", None, run, every=every),)
    return calls


def test_interval_due_predicate():
    every = timedelta(minutes=30)
    assert is_due_every(every, JobState(), _now(TODAY, 3, 0)), (
        "no attempt on record: due on the first tick, whatever the clock says"
    )
    ran = JobState(last_attempt_at=_now(TODAY, 4, 0))
    assert not is_due_every(every, ran, _now(TODAY, 4, 29))
    assert is_due_every(every, ran, _now(TODAY, 4, 30))


async def test_interval_job_runs_on_its_cadence(database, sched):
    calls = _patch_interval_job(sched, 0)
    await sched.load_state()
    await sched.tick(_now(TODAY, 3, 0))  # the nightly time gate is not its
    assert calls == ["i"]
    await sched.tick(_now(TODAY, 3, 29))
    assert calls == ["i"]
    await sched.tick(_now(TODAY, 3, 30))
    assert calls == ["i", "i"]


async def test_interval_pacing_survives_a_restart(database, sched):
    calls = _patch_interval_job(sched, 0)
    await sched.record(
        "i", exit_code=0, success_on=TODAY, attempted_at=_now(TODAY, 4, 0)
    )
    await sched.load_state()  # hydrates last_attempt_at
    await sched.tick(_now(TODAY, 4, 10))
    assert calls == [], "a crash-looping container cannot hammer the API"
    await sched.tick(_now(TODAY, 4, 30))
    assert calls == ["i"]


async def test_interval_failure_alerts_once_a_day_but_keeps_retrying(
    database, sent_mail, sched
):
    _alerting(sched)
    calls = _patch_interval_job(sched, 1)
    await sched.load_state()
    await sched.tick(_now(TODAY, 4, 0))
    await sched.tick(_now(TODAY, 4, 30))
    await sched.tick(_now(TODAY, 5, 0))
    assert len(calls) == 3, "MAX_ATTEMPTS_PER_DAY never throttles the cadence"
    assert [(to, subject) for to, subject, _ in sent_mail] == [
        ("ops@example.org", "[volunteerdb] i FAILED")
    ], "one alert for the day, not one per failure"
    await sched.tick(_now(TODAY + timedelta(days=1), 4, 0))
    assert len(sent_mail) == 2, "a new parish day alerts afresh"


# --- the advisory job lock ----------------------------------------------------


async def test_lock_holder_excludes_the_scheduler(database, sched):
    calls = _patch_jobs(sched, {"a": 0})
    await sched.load_state()
    async with job_lock(sched.env, "a") as held:  # a manual `python -m` run in flight
        assert held
        await sched.tick(_now(TODAY, 4, 30))
    assert calls == []
    assert sched.state["a"].attempts == 0  # a lock skip burns no attempt
    assert await _row("a") is None
    await sched.tick(_now(TODAY, 4, 31))  # released: next tick runs it
    assert calls == ["a"]


# --- the real registry --------------------------------------------------------


def test_registry_mixes_nightly_and_interval_jobs():
    by_name = {j.name: j for j in scheduler.JOBS}
    for name in ("roster_sync", "fetch_pages", "proposal_digest", "event_reminders"):
        assert by_name[name].every is None and by_name[name].at_setting
    sync = by_name["calendar_sync"]
    assert (sync.at_setting, sync.every) == (None, timedelta(minutes=30))
    cleanup = by_name["task_force_cleanup"]
    assert (cleanup.at_setting, cleanup.every) == (None, timedelta(hours=1))
    assert all(j.every is not None for j in scheduler.JOBS[4:]), (
        "interval jobs come last so they never delay the nightly chain"
    )


# --- the activation seam ------------------------------------------------------


async def test_create_app_does_not_start_scheduler(real_app_client):
    assert getattr(app.state, "scheduler", None) is None, (
        "main.run builds and starts the loop; create_app() never does"
    )
