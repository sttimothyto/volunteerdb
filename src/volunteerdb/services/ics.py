"""iCalendar (RFC 5545) feeds of the parish's events.

Written by hand rather than through a library: a feed here is a header and
one VEVENT per entry, some sixty lines of format, and the two rules that
actually bite -- text escaping and the 75-octet line fold -- are the kind a
dependency hides and a test states. Times go out in UTC ("Z" form), which
every client converts for itself and which needs no VTIMEZONE block; the
parish zone rides along as X-WR-TIMEZONE for the clients that read it.

Cancelled events are simply absent: a subscribed client drops a UID that
has left the feed, whereas STATUS:CANCELLED would keep the corpse visible.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .events import CalendarEntry

PRODID = "-//VolunteerDB//EN"
REFRESH = "PT1H"
FOLD_AT = 75  # octets, per RFC 5545 §3.1
CRLF = "\r\n"


def _escape(text: str) -> str:
    """TEXT values: backslash, semicolon and comma are delimiters; a newline
    is spelled out. Carriage returns have no place in a value at all."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Break a content line into 75-octet pieces, continuation lines led by a
    space. Counted in octets of the UTF-8 form but split only between
    characters, so a multi-byte character is never cut in half."""
    if len(line.encode()) <= FOLD_AT:
        return line
    out: list[str] = []
    piece, size, limit = "", 0, FOLD_AT
    for ch in line:
        width = len(ch.encode())
        if size + width > limit:
            out.append(piece)
            piece, size, limit = " ", 1, FOLD_AT
        piece += ch
        size += width
    out.append(piece)
    return CRLF.join(out)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _vevent(
    entry: CalendarEntry, *, host: str, base_url: str, now: datetime
) -> list[str]:
    event = entry.event
    summary = event.title
    if entry.slot_name:
        summary = f"{summary} — {entry.slot_name}"
    lines = [
        "BEGIN:VEVENT",
        f"UID:vdb-event-{event.id}@{host}",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(event.starts_at)}",
        f"DTEND:{_stamp(event.ends_at)}",
        f"SUMMARY:{_escape(summary)}",
    ]
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    description = event.description or ""
    if entry.path:
        description = f"{entry.path}\n{description}" if description else entry.path
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines.append(f"URL:{base_url}/events/{event.id}")
    lines.append("END:VEVENT")
    return lines


def render(
    entries: list[CalendarEntry],
    *,
    name: str,
    host: str,
    base_url: str,
    now: datetime,
    tz: ZoneInfo,
) -> bytes:
    """The whole feed, CRLF-terminated, folded, UTF-8. `now` stamps every
    entry; `tz` is the parish zone the feed declares."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
        f"X-WR-TIMEZONE:{tz.key}",
        f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH}",
        f"X-PUBLISHED-TTL:{REFRESH}",
    ]
    for entry in entries:
        lines.extend(_vevent(entry, host=host, base_url=base_url, now=now))
    lines.append("END:VCALENDAR")
    return (CRLF.join(_fold(line) for line in lines) + CRLF).encode()


# The feed window: enough past for "what did I do last month", and beyond a
# year ahead so a weekly series stays whole.
WINDOW_BACK = timedelta(days=60)
WINDOW_FORWARD = timedelta(days=400)
