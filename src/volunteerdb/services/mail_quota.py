"""How much of the mail provider's allowance this instance has spent.

The SMTP2GO free tier is 200 messages a day and 1,000 a month, and the app has
no way to ask for more once a window is exhausted: the next send simply fails,
which on this instance means a sign-in code that never arrives. So every
message that actually leaves counts itself into `mail_quota` — one row per
parish day, upserted — and `projection()` turns those counters into the two
numbers an admin can act on: whether a *day* is on course to cross 200, and
whether the *month* is on course to cross 1,000.

Two design notes worth the words:

*The day is the parish day* (`settings().timezone`), not the provider's clock,
because the provider's reset hour is not documented and an admin reasons in
local days anyway. That makes this an early-warning gauge, not an accounting
system — it is allowed to disagree with the SMTP2GO dashboard by a few hours'
worth of mail near midnight, and the caps are approached with room to spare.

*One sender is not counted*: the host's nightly backup wrapper
(`deploy/templates/volunteerdb-backup.sh.j2`) posts its own failure alert to
SMTP2GO with curl, outside this process entirely. It is at most one message a
night and only when a backup fails, so the gauge reads slightly low exactly
when something else is already wrong — which is the safe direction for a
warning, but worth knowing before trusting the figure to the message.

*The daily figure is never extrapolated from the hour.* The nightly jobs fire
at 03:30 and 04:00, so the bulk of a normal day's mail is already out before
breakfast; scaling "80 sent by 04:00" up by the fraction of the day remaining
would project 480 and cry wolf every morning. Instead a day is judged on the
worst *complete* day in the trailing week (plus today's running total, in case
today is already the worst) — which answers the real question, "does this
instance's pattern produce 200-message days?", without pretending to know when
in the day the mail goes.
"""

import calendar
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import settings
from ..db import db_session
from ..models import MailQuota

log = structlog.get_logger(__name__)

# SMTP2GO free tier. Literals, not settings: they are the provider's terms, and
# an instance that moves to a paid plan should edit them here (one place) with
# the docs, rather than carry a knob nobody turns.
DAILY_CAP = 200
MONTHLY_CAP = 1000

# Complete days averaged for the monthly run-rate, and scanned for the worst
# day. A week covers the parish's real cycle: liturgical rosters are weekly, so
# a shorter window would read a quiet Tuesday as the norm.
TRAILING_DAYS = 7

# projection() is read on every admin page load; recomputing it per request
# would put two counts in front of every navigation for no new information.
CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class Projection:
    """What the counters say, and how loudly to say it."""

    today: int  # messages sent so far on the parish day
    month_to_date: int
    peak_day: int  # worst day in the trailing week, or today if today is worse
    projected_month: int  # month-to-date plus the trailing run-rate to month end
    level: str  # "" (quiet) | "warning" (on course to cross) | "critical" (crossed)
    reason: str  # one clause naming which cap, for the banner

    @property
    def alarming(self) -> bool:
        return bool(self.level)


def days_in_month(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def project(counts: Mapping[date, int], today: date) -> Projection:
    """Pure — unit-tested. `counts` is every day the ledger holds from the
    trailing window and the current month; missing days count as zero.

    The run-rate averages only days the ledger could have seen. A brand-new
    instance has one day of history, and dividing its 40 messages by seven
    would project 170 for the month and stay silent right up to the cliff.
    """
    sent_today = counts.get(today, 0)
    month_to_date = sum(
        n
        for day, n in counts.items()
        if day.year == today.year and day.month == today.month and day <= today
    )

    # complete days only: today is partial and would drag the average down
    earliest = min(counts) if counts else today
    window_start = max(earliest, today - timedelta(days=TRAILING_DAYS))
    complete = [
        window_start + timedelta(days=n) for n in range((today - window_start).days)
    ]
    if complete:
        rate = sum(counts.get(day, 0) for day in complete) / len(complete)
        peak_day = max([sent_today] + [counts.get(day, 0) for day in complete])
    else:
        # first day of counting: today's running total is the only evidence there is
        rate = float(sent_today)
        peak_day = sent_today

    days_left = days_in_month(today) - today.day
    projected_month = month_to_date + round(rate * days_left)

    # "critical" is a window already spent; "warning" is one on course to be.
    if sent_today >= DAILY_CAP:
        level, reason = (
            "critical",
            f"today's {sent_today} messages have reached the {DAILY_CAP}/day limit",
        )
    elif month_to_date >= MONTHLY_CAP:
        level, reason = (
            "critical",
            f"this month's {month_to_date:,} messages have reached the "
            f"{MONTHLY_CAP:,}/month limit",
        )
    elif peak_day >= DAILY_CAP:
        level, reason = (
            "warning",
            f"a day this week sent {peak_day} messages, at the {DAILY_CAP}/day limit",
        )
    elif projected_month >= MONTHLY_CAP:
        level, reason = (
            "warning",
            f"at this week's rate the month reaches about {projected_month:,} "
            f"messages, over the {MONTHLY_CAP:,}/month limit",
        )
    else:
        level, reason = "", ""

    return Projection(
        today=sent_today,
        month_to_date=month_to_date,
        peak_day=peak_day,
        projected_month=projected_month,
        level=level,
        reason=reason,
    )


def local_today() -> date:
    """Today in the parish's day (settings().timezone), not the container's.

    Two lines duplicated from `elections.local_today` on purpose: this module
    is infrastructure and that one is the elections domain, and a services ->
    services import to save them would be the more expensive coupling.
    """
    return datetime.now(ZoneInfo(settings().timezone)).date()


async def record_send(when: date | None = None) -> None:
    """Count one message that actually left. Never raises.

    Called from `mail.send_email` on the success path only: a message the
    provider rejected consumed no allowance, and a ledger that counted attempts
    would raise the alarm hardest exactly when nothing was being sent.

    A failure here must never turn into a failed send — the counter exists to
    protect the mail, not the other way round — so everything is swallowed and
    logged.
    """
    day = when or local_today()
    try:
        async with db_session() as session:
            await session.execute(
                pg_insert(MailQuota)
                .values(day=day, sent=1)
                .on_conflict_do_update(
                    index_elements=["day"],
                    set_={"sent": MailQuota.__table__.c.sent + 1},
                )
            )
    except Exception:  # noqa: BLE001 — see the docstring
        log.warning("mail_quota.record_failed", day=str(day), exc_info=True)


async def read_counts(today: date) -> dict[date, int]:
    """Every ledger row the projection can use: this month, plus the trailing
    week where it reaches back into the last one."""
    start = min(today.replace(day=1), today - timedelta(days=TRAILING_DAYS))
    async with db_session() as session:
        rows = await session.execute(
            sa.select(MailQuota.day, MailQuota.sent).where(
                MailQuota.day >= start, MailQuota.day <= today
            )
        )
    return {day: sent for day, sent in rows}


_cache: tuple[float, Projection] | None = None


def clear_cache() -> None:
    """Drop the memo. For tests, and for anything that has just changed the
    ledger and wants the next read to see it."""
    global _cache
    _cache = None


async def projection() -> Projection:
    """The cached gauge every admin page header reads. Never raises: a banner
    that cannot be computed is simply not shown."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    try:
        today = local_today()
        result = project(await read_counts(today), today)
    except Exception:  # noqa: BLE001 — a gauge must not break the page it sits on
        log.warning("mail_quota.projection_failed", exc_info=True)
        return Projection(0, 0, 0, 0, "", "")
    _cache = (now, result)
    return result


def support_contact() -> str:
    """Who an admin should tell. VDB_SUPPORT_CONTACT, else the address that
    already receives job-failure alerts, else "" — the banner drops the clause
    rather than naming nobody."""
    s = settings()
    return (s.support_contact or s.alert_email).strip()
