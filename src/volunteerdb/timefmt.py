"""How a moment or a span reads in words: the parish's clock, in prose.

Pure formatting, shared by the mail templates and the pages, so a duty
reads the same in a digest and on the events page. `tz` is always the
parish's zone (Env.tz), never the host's.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def when_short(at: datetime, tz: ZoneInfo) -> str:
    """'Thu, Sep 10, 7:30 PM': the one short form for a table cell, a badge
    or a line that names an instant, in the parish's clock."""
    local = at.astimezone(tz)
    return f"{local:%a, %b %-d}, {local:%-I:%M %p}"


def day(at: datetime, tz: ZoneInfo) -> str:
    """'Sep 10, 2026': the day an instant fell on, in the parish's clock."""
    return date_words(at.astimezone(tz).date())


def date_words(on: date) -> str:
    """'Sep 13, 2026' for a date that is already a parish day (a deadline)."""
    return f"{on:%b %-d, %Y}"


def clock(at: datetime, tz: ZoneInfo) -> str:
    """'7:30 PM': the time of day alone, where the day is already said."""
    return f"{at.astimezone(tz):%-I:%M %p}"


def event_when(starts_at: datetime, ends_at: datetime, tz: ZoneInfo) -> str:
    """'Sunday, August 23, 2026, 10:30 AM–12:00 PM' in the parish's clock."""
    s, e = starts_at.astimezone(tz), ends_at.astimezone(tz)
    if e.date() == s.date():
        return f"{s:%A, %B %-d, %Y}, {s:%-I:%M %p}–{e:%-I:%M %p}"
    return f"{s:%A, %B %-d, %Y}, {s:%-I:%M %p} – {e:%A, %B %-d, %Y}, {e:%-I:%M %p}"


def ttl_window(hours: int) -> str:
    """'24 hours' / '7 days' — whole multiples of a day read as days."""
    if hours > 24 and hours % 24 == 0:
        return f"{hours // 24} days"
    return f"{hours} hours"
