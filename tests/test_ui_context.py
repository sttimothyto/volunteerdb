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


def test_parse_as_of_only_bumps_a_bare_date():
    """fromisoformat accepts 'T', a lowercase 't' and a space as the separator,
    so a case-sensitive 'T' check hands back a whole extra day to anyone who
    asked for midnight explicitly."""
    for explicit in (
        "2024-03-05T00:00:00",
        "2024-03-05t00:00:00",
        "2024-03-05 00:00:00",
    ):
        parsed = parse_as_of(explicit)
        assert parsed is not None and parsed.time() == time.min, (
            f"{explicit!r} names midnight explicitly and must be taken literally"
        )

    bare = parse_as_of("2024-03-05")
    assert bare is not None and bare.time() == time(23, 59, 59, 999999)


def test_parse_as_of_rejects_garbage():
    assert parse_as_of("") is None
    assert parse_as_of("   ") is None
    assert parse_as_of("not-a-date") is None
    assert parse_as_of("2024-13-45") is None
    assert parse_as_of(None) is None
