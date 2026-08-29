"""The iCalendar writer: the two rules that bite (text escaping, the 75-octet
fold), UTC times, stable UIDs, and what a personal entry carries beyond a
parish one. Pure functions over transient Event rows — no database."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.models import Event
from volunteerdb.services import ics
from volunteerdb.services.events import CalendarEntry

pytestmark = pytest.mark.pure

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _event(**overrides) -> Event:
    fields = dict(
        id=7,
        team_id=1,
        title="Sunday Mass",
        description=None,
        location="Main church",
        starts_at=datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        ends_at=datetime(2026, 9, 6, 12, 0, tzinfo=TZ),
    )
    return Event(**{**fields, **overrides})


def _render(*entries: CalendarEntry, name="Parish events") -> str:
    return ics.render(
        list(entries),
        name=name,
        host="vdb.example.org",
        base_url="https://vdb.example.org",
        now=NOW,
    ).decode()


def _unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_a_parish_entry_is_a_public_event_in_utc():
    text = _render(CalendarEntry(_event(), None, "Liturgy", True))
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", ""), "every line ends in CRLF"
    lines = _unfold(text)
    assert "X-WR-CALNAME:Parish events" in lines
    assert "X-WR-TIMEZONE:America/Toronto" in lines
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT1H" in lines
    assert "UID:vdb-event-7@vdb.example.org" in lines
    assert "DTSTAMP:20260824T120000Z" in lines
    assert "DTSTART:20260906T140000Z" in lines, "10:00 Toronto (EDT) is 14:00Z"
    assert "DTEND:20260906T160000Z" in lines
    assert "SUMMARY:Sunday Mass" in lines
    assert "LOCATION:Main church" in lines
    assert "DESCRIPTION:Liturgy" in lines, "the team path is the description"
    assert "URL:https://vdb.example.org/events/7" in lines
    assert "STATUS" not in text and "ATTENDEE" not in text


def test_a_personal_entry_names_the_slot():
    text = _render(CalendarEntry(_event(), "Lector", "Liturgy", True))
    assert "SUMMARY:Sunday Mass — Lector" in _unfold(text)


def test_text_is_escaped_and_the_description_keeps_its_lines():
    event = _event(
        title="Coffee, tea; & \\cake",
        location="Hall (north; door)",
        description="Bring:\r\n- cups\n- milk, sugar",
    )
    lines = _unfold(_render(CalendarEntry(event, None, "Social", True)))
    assert "SUMMARY:Coffee\\, tea\\; & \\\\cake" in lines
    assert "LOCATION:Hall (north\\; door)" in lines
    assert "DESCRIPTION:Social\\nBring:\\n- cups\\n- milk\\, sugar" in lines


def test_long_lines_fold_at_75_octets_between_characters():
    title = "Célébration " * 12  # multi-byte, well past one line
    text = _render(CalendarEntry(_event(title=title.strip()), None, "", True))
    raw_lines = text.split("\r\n")
    assert all(len(line.encode()) <= 75 for line in raw_lines), "no line over 75 octets"
    folded = [line for line in raw_lines if line.startswith(" ")]
    assert folded, "the title had to fold"
    assert f"SUMMARY:{title.strip()}" in _unfold(text), "unfolding restores it"
    for line in raw_lines:
        line.encode().decode()  # a split inside a character would not survive


def test_uids_are_stable_and_the_feed_orders_nothing_itself():
    a = CalendarEntry(_event(id=3), None, "", True)
    b = CalendarEntry(_event(id=4, title="Vespers"), None, "", True)
    first, second = _render(a, b), _render(a, b)
    assert first == second, "same input, same bytes — clients diff on UID"
    assert first.index("UID:vdb-event-3@") < first.index("UID:vdb-event-4@"), (
        "entries come out in the order the service gave them"
    )


def test_the_calendar_name_is_escaped_too():
    text = _render(name="St. Timothy's; events")
    assert "X-WR-CALNAME:St. Timothy's\\; events" in _unfold(text)
    assert "BEGIN:VEVENT" not in text
