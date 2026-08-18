"""Parsing the `?as_of=` query parameter, shared by both front doors.

Both surfaces accept the same query string, so both must resolve it to the same
instant. They did not: the API annotated as_of as a datetime and let FastAPI
parse a bare date to midnight, while the GUI bumped it to the end of the day —
the same '?as_of=2026-07-30' returning snapshots ~24 hours apart.

A bare date means the END of that day. Asking for the state "as of July 30"
should include what happened on July 30, not stop at its first instant.
"""

from datetime import datetime, time, timedelta

_TO_END_OF_DAY = timedelta(days=1) - timedelta(microseconds=1)


def _names_a_time(text: str) -> bool:
    """datetime.fromisoformat accepts 'T', 't' and ' ' as the date/time
    separator, so checking for an uppercase 'T' alone misreads two of the three."""
    return "t" in text.casefold() or " " in text


def parse_as_of(raw: str | None) -> datetime:
    """An ISO date or timestamp as an aware datetime; ValueError on anything else.

    A bare date is bumped to the last microsecond of that day; an explicitly
    written time is taken literally, midnight included. Naive values are
    interpreted in the server's local timezone.
    """
    text = (raw or "").strip()
    parsed = datetime.fromisoformat(text)  # raises ValueError on garbage
    if parsed.time() == time.min and not _names_a_time(text):
        parsed += _TO_END_OF_DAY
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed
