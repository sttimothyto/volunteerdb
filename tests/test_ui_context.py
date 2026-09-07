"""Unit tests for the as-of query-param parser."""

from datetime import UTC, date, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.ui.context import parse_as_of

pytestmark = pytest.mark.pure

TZ = ZoneInfo("America/Toronto")


def test_parse_as_of_date_maps_to_end_of_day():
    parsed = parse_as_of("2024-03-05", TZ)
    assert parsed is not None
    assert parsed.date() == date(2024, 3, 5)
    assert parsed.time() == time(23, 59, 59, 999999)
    assert parsed.tzinfo is not None, "naive values get the parish timezone"

    # an explicit midnight with a T component is taken literally
    exact = parse_as_of("2024-03-05T00:00", TZ)
    assert exact.time() == time.min
    assert exact.tzinfo is not None

    with_tz = parse_as_of("2024-03-05T12:30:00+00:00", TZ)
    assert with_tz.utcoffset().total_seconds() == 0


def test_a_naive_value_is_read_in_the_parish_zone_not_the_hosts():
    """CI runs on a UTC host and the parish is in Toronto; read on the host's
    clock, 'as of today' ended at 8 pm Toronto time and hid the evening's work.
    Two zones on opposite sides of UTC: whichever the host is in, at least one
    of these is not it."""
    east = parse_as_of("2024-03-05", ZoneInfo("Pacific/Kiritimati"))
    west = parse_as_of("2024-03-05", ZoneInfo("Etc/GMT+12"))
    assert east.utcoffset() == timedelta(hours=14)
    assert west.utcoffset() == timedelta(hours=-12)
    assert east.astimezone(UTC) < west.astimezone(UTC), (
        "the same calendar day ends 26 hours later on the far side of the line"
    )


def test_parse_as_of_only_bumps_a_bare_date():
    """fromisoformat accepts 'T', a lowercase 't' and a space as the separator,
    so a case-sensitive 'T' check hands back a whole extra day to anyone who
    asked for midnight explicitly."""
    for explicit in (
        "2024-03-05T00:00:00",
        "2024-03-05t00:00:00",
        "2024-03-05 00:00:00",
    ):
        parsed = parse_as_of(explicit, TZ)
        assert parsed is not None and parsed.time() == time.min, (
            f"{explicit!r} names midnight explicitly and must be taken literally"
        )

    bare = parse_as_of("2024-03-05", TZ)
    assert bare is not None and bare.time() == time(23, 59, 59, 999999)


def test_parse_as_of_rejects_garbage():
    assert parse_as_of("", TZ) is None
    assert parse_as_of("   ", TZ) is None
    assert parse_as_of("not-a-date", TZ) is None
    assert parse_as_of("2024-13-45", TZ) is None
    assert parse_as_of(None, TZ) is None


def test_as_of_query_reads_back_as_the_same_instant():
    """The header carries a snapshot on its links as the shortest value
    parse_as_of maps back to it: a bare date for the end of a parish day
    (what a typed date means), the full timestamp otherwise."""
    from urllib.parse import unquote

    from volunteerdb.ui.asof import as_of_query

    tz = ZoneInfo("America/Toronto")
    typed = parse_as_of("2026-07-30", tz)
    assert typed is not None
    assert as_of_query(typed) == "2026-07-30"
    assert parse_as_of(as_of_query(typed), tz) == typed

    exact = parse_as_of("2026-07-30T10:15:00-04:00", tz)
    assert exact is not None
    query = as_of_query(exact)
    assert "T10:15" in unquote(query) and "+" not in query and " " not in query
    assert parse_as_of(unquote(query), tz) == exact
