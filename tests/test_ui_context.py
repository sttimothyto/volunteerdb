"""Unit tests for the as-of query-param parser."""

from datetime import date, time

from volunteerdb.ui.context import parse_as_of


def test_parse_as_of_date_maps_to_end_of_day():
    parsed = parse_as_of("2024-03-05")
    assert parsed is not None
    assert parsed.date() == date(2024, 3, 5)
    assert parsed.time() == time(23, 59, 59, 999999)
    assert parsed.tzinfo is not None, "naive values get the local timezone"

    # an explicit midnight with a T component is taken literally
    exact = parse_as_of("2024-03-05T00:00")
    assert exact.time() == time.min
    assert exact.tzinfo is not None

    with_tz = parse_as_of("2024-03-05T12:30:00+00:00")
    assert with_tz.utcoffset().total_seconds() == 0


def test_parse_as_of_rejects_garbage():
    assert parse_as_of("") is None
    assert parse_as_of("   ") is None
    assert parse_as_of("not-a-date") is None
    assert parse_as_of("2024-13-45") is None
    assert parse_as_of(None) is None
