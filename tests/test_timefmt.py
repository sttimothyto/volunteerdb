"""The parish's clock, in words: the short forms every page and badge use.

All in the parish's zone, never the host's (the timezone CI failures of
2026-09-03 were host-zone leaks); the instants below are UTC and read back
in Toronto."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from volunteerdb import timefmt

pytestmark = pytest.mark.pure

TORONTO = ZoneInfo("America/Toronto")
# 23:30 UTC on Sep 10 is 7:30 PM in Toronto -- still the 10th there
AT = datetime(2026, 9, 10, 23, 30, tzinfo=UTC)


def test_the_short_form_names_weekday_day_and_time():
    assert timefmt.when_short(AT, TORONTO) == "Thu, Sep 10, 7:30 PM"


def test_the_day_form_and_the_clock_form():
    assert timefmt.day(AT, TORONTO) == "Sep 10, 2026"
    assert timefmt.clock(AT, TORONTO) == "7:30 PM"
    # an instant that is the next day in UTC is still today's in the parish
    late = datetime(2026, 9, 11, 3, 5, tzinfo=UTC)
    assert timefmt.day(late, TORONTO) == "Sep 10, 2026"
    assert timefmt.clock(late, TORONTO) == "11:05 PM"


def test_a_deadline_is_a_parish_day_already():
    assert timefmt.date_words(date(2026, 9, 13)) == "Sep 13, 2026"
