"""The mail-allowance ledger and the projection it feeds.

The provider allows 200 messages a day and 1,000 a month; past that a send
just fails, and on this instance that is a sign-in code that never arrives.
`project()` is pure, so most of this is table-driven arithmetic — the parts
worth pinning are the ones a naive implementation gets wrong: an instance one
day old must not divide its first day by a week and stay silent, and the
nightly-job front-loading must not make every morning look like an emergency.
"""

from datetime import date, timedelta

import sqlalchemy as sa

from volunteerdb.models import MailQuota
from volunteerdb.services import mail_quota

from tests.conftest import db_session
from tests.fakes import RaisingSessions

MAY = date(2026, 5, 15)  # a 31-day month, halfway through


def _week_before(day: date, n: int) -> dict[date, int]:
    """n messages on each of the seven complete days before `day`."""
    return {day - timedelta(days=k): n for k in range(1, 8)}


# --- projection arithmetic ----------------------------------------------------


def test_a_quiet_instance_says_nothing():
    counts = _week_before(MAY, 5) | {MAY: 3}
    result = mail_quota.project(counts, MAY)
    assert result.level == ""
    assert not result.alarming
    assert (result.today, result.peak_day) == (3, 5)
    # 38 so far this month, 5/day for the 16 days left
    assert result.projected_month == 38 + 5 * 16


def test_a_weekly_roster_that_produces_two_hundred_message_days_warns():
    """The day check reads the worst COMPLETE day, not the clock.

    A digest that goes out at 04:00 has spent the day's mail before breakfast;
    extrapolating from the hour would cry wolf every morning, so the question
    asked instead is whether this instance's pattern makes 200-message days.
    """
    counts = _week_before(MAY, 20) | {MAY - timedelta(days=2): 205, MAY: 4}
    result = mail_quota.project(counts, MAY)
    assert result.level == "warning"
    assert result.peak_day == 205
    assert "200/day" in result.reason


def test_a_steady_rate_that_overshoots_the_month_warns():
    """No single day is near 200; the month is the thing that runs out.

    50 a day is an unremarkable parish day, and 40 a day would still land
    inside the month — the monthly cap is what a comfortable daily rate
    quietly crosses, which is why both are checked.
    """
    counts = _week_before(MAY, 50) | {MAY: 10}
    result = mail_quota.project(counts, MAY)
    assert result.peak_day == 50, "well inside the daily cap"
    assert result.projected_month == 360 + 50 * 16  # 1,160
    assert result.level == "warning"
    assert "1,000/month" in result.reason

    # the same shape one notch quieter stays silent: 930 projected, no banner
    assert mail_quota.project(_week_before(MAY, 40) | {MAY: 10}, MAY).level == ""


def test_a_spent_day_is_critical_not_a_warning():
    result = mail_quota.project(_week_before(MAY, 5) | {MAY: 200}, MAY)
    assert result.level == "critical"
    assert "today's 200" in result.reason


def test_a_spent_month_is_critical():
    counts = {MAY - timedelta(days=k): 80 for k in range(0, 14)}
    result = mail_quota.project(counts, MAY)
    assert result.month_to_date == 80 * 14
    assert result.level == "critical"
    assert "1,000/month" in result.reason


def test_a_brand_new_instance_is_judged_on_the_day_it_has():
    """One day of history must not be averaged over seven.

    Dividing a first day's 90 messages by a week projects 170 for the month
    and stays quiet right up to the cliff — the failure mode this exists to
    avoid.
    """
    result = mail_quota.project({MAY: 90}, MAY)
    assert result.projected_month == 90 + 90 * 16
    assert result.level == "warning"


def test_only_this_month_counts_toward_the_monthly_figure():
    first = date(2026, 6, 2)
    counts = {date(2026, 5, 30): 500, date(2026, 5, 31): 500, first: 10}
    result = mail_quota.project(counts, first)
    assert result.month_to_date == 10, "last month's spend is last month's"
    # but the trailing week still sets the run-rate, and 1,010 over 5 complete
    # days is far past a 1,000-message June
    assert result.level == "warning"


def test_days_are_never_double_counted_or_projected_past_month_end():
    last = date(2026, 5, 31)
    result = mail_quota.project(_week_before(last, 10) | {last: 10}, last)
    assert result.projected_month == 80, "no days left to project into"


def test_missing_days_are_zero_not_absent():
    counts = {MAY - timedelta(days=7): 700, MAY: 1}
    result = mail_quota.project(counts, MAY)
    assert result.peak_day == 700
    assert result.level == "warning"


# --- the ledger ---------------------------------------------------------------


async def test_record_counts_one_row_per_parish_day(env):
    day = date(2026, 3, 4)
    for _ in range(3):
        await env.quota.record(env.sessions, day)
    await env.quota.record(env.sessions, day + timedelta(days=1))

    async with db_session() as session:
        rows = dict(
            (await session.execute(sa.select(MailQuota.day, MailQuota.sent))).all()
        )
    assert rows == {day: 3, day + timedelta(days=1): 1}


async def test_read_counts_spans_the_month_and_the_trailing_week(env):
    today = date(2026, 6, 3)  # early enough that the week reaches into May
    for day, n in {
        date(2026, 5, 20): 1,  # outside the window entirely
        date(2026, 5, 28): 2,  # in the trailing week, last month
        date(2026, 6, 1): 3,
        date(2026, 6, 3): 4,
        date(2026, 6, 9): 5,  # the future: a manual run cannot pull it in
    }.items():
        for _ in range(n):
            await env.quota.record(env.sessions, day)

    async with db_session() as session:
        counts = await mail_quota.read_counts(session, today)
    assert counts == {date(2026, 5, 28): 2, date(2026, 6, 1): 3, date(2026, 6, 3): 4}


async def test_projection_is_memoised_and_droppable(env):
    """The gauge is memoised for a minute in the Env's cell -- one holder,
    dropped on demand -- never in this module."""
    cell, today, now = env.quota, env.today(), env.clock.now()
    await env.quota.record(env.sessions, today)
    assert (await cell.projection(env.sessions, today, now)).today == 1

    await env.quota.record(env.sessions, today)
    assert (await cell.projection(env.sessions, today, now)).today == 1, (
        "served from the memo"
    )

    cell.reset()
    assert (await cell.projection(env.sessions, today, now)).today == 2


async def test_the_cell_records_and_never_raises(env):
    day = date(2026, 3, 4)
    await env.quota.record(env.sessions, day)
    await env.quota.record(env.sessions, day)
    async with db_session() as session:
        assert dict(
            (await session.execute(sa.select(MailQuota.day, MailQuota.sent))).all()
        ) == {day: 2}

    # the database boundary gone: the counter exists to protect the mail,
    # not the other way round, so nothing escapes
    await env.quota.record(RaisingSessions(), day)
    assert (
        await env.quota.projection(RaisingSessions(), day, env.clock.now())
    ).level == "", "a gauge that cannot be computed is simply not shown"
