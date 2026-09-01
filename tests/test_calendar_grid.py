"""The month grid's arithmetic and its escaping, without a page: month
rollover in both directions, a leap February, a typo in the address bar,
and a user-typed title that must not become markup."""

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.ui import calendar_grid as grid

pytestmark = pytest.mark.pure

TZ = ZoneInfo("America/Toronto")


@pytest.mark.parametrize(
    ("month", "delta", "expected"),
    [
        (date(2026, 12, 1), 1, date(2027, 1, 1)),
        (date(2026, 1, 1), -1, date(2025, 12, 1)),
        (date(2026, 3, 31), 0, date(2026, 3, 1)),
        (date(2026, 6, 15), 12, date(2027, 6, 1)),
        (date(2026, 6, 15), -18, date(2024, 12, 1)),
    ],
)
def test_shift_month_crosses_years_and_lands_on_the_first(month, delta, expected):
    assert grid.shift_month(month, delta) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-03", date(2026, 3, 1)),
        ("2026-13", date(2026, 9, 1)),  # no thirteenth month
        ("garbage", date(2026, 9, 1)),
        ("", date(2026, 9, 1)),
        ("2026-03-15", date(2026, 9, 1)),  # a day is not a month
    ],
)
def test_parse_month_falls_back_to_the_default_month(text, expected):
    assert grid.parse_month(text, default=date(2026, 9, 17)) == expected


@pytest.mark.parametrize(
    ("month", "first", "last"),
    [
        (date(2024, 2, 1), date(2024, 1, 28), date(2024, 3, 2)),  # leap February
        (date(2026, 2, 1), date(2026, 2, 1), date(2026, 2, 28)),  # a Sunday start
        (date(2026, 12, 1), date(2026, 11, 29), date(2027, 1, 2)),
    ],
)
def test_grid_bounds_are_whole_weeks_starting_sunday(month, first, last):
    assert grid.grid_bounds(month) == (first, last)
    assert first.weekday() == 6 and last.weekday() == 5


def _entry(title: str, starts_at: datetime, visible: bool = True):
    event = SimpleNamespace(id=7, title=title, starts_at=starts_at)
    return SimpleNamespace(
        event=event, visible=visible, slot_name="Lector", path="Liturgy"
    )


def test_the_grid_escapes_what_people_typed_and_marks_today():
    when = datetime(2026, 9, 6, 10, 30, tzinfo=TZ)
    html = grid.month_grid(
        [_entry("<b>Mass</b> & more", when)],
        date(2026, 9, 1),
        today=date(2026, 9, 6),
        tz=TZ,
        prev_href="/events?month=2026-08&x=<y>",
        next_href="/events?month=2026-10",
        empty_note="nothing",
    )
    assert "&lt;b&gt;Mass&lt;/b&gt; &amp; more" in html and "<b>Mass</b>" not in html
    assert 'href="/events?month=2026-08&amp;x=&lt;y&gt;"' in html
    assert html.count('aria-current="date"') == 1
    assert '<time datetime="2026-09-06T10:30-04:00">10:30</time>' in html
    assert "nothing" not in html, "the empty note only shows for an empty month"
    assert "September 2026" in html and "August" in html and "October" in html


def test_an_event_outside_the_readers_view_shows_when_but_not_where_to_go():
    when = datetime(2026, 9, 6, 10, 30, tzinfo=TZ)
    html = grid.month_grid(
        [_entry("Private", when, visible=False)],
        date(2026, 9, 1),
        today=date(2026, 9, 1),
        tz=TZ,
        prev_href="/p",
        next_href="/n",
        empty_note="",
    )
    assert 'class="vdb-cal-title">Private</span>' in html
    assert 'href="/events/7"' not in html


def test_the_view_switch_marks_the_current_view():
    html = grid.view_switch(
        "parish", {"mine": "/events?view=mine", "parish": "/events?view=parish&a=<b>"}
    )
    assert html.count('aria-current="page"') == 1
    assert (
        'href="/events?view=parish&amp;a=&lt;b&gt;" aria-current="page">Whole parish'
        in html
    )
