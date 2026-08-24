"""The month grid on /events, and the switch between its two views.

Plain HTML, rendered on the server and handed to ui.html: a <table> whose
cells are days, a <nav> of links for the month before and after, and a
<nav> of two links for "my duties" / "whole parish". No JavaScript is
involved at any point — a link is what changes the month or the view — so
the grid works over a plain GET, reads as a table to a screen reader, and
re-flows into a list on a narrow screen through CSS alone (theme.css,
.vdb-cal). Everything user-typed passes through html.escape.
"""

import calendar
import html
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..services.events import CalendarEntry

VIEWS = (("mine", "My duties"), ("parish", "Whole parish"))
# Sunday first: the week a parish lives by, and what every wall calendar
# in the country shows
FIRST_WEEKDAY = calendar.SUNDAY
DAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


def grid_bounds(month: date) -> tuple[date, date]:
    """First and last day the grid for `month` shows — whole weeks, so a few
    days of the neighbouring months ride along."""
    weeks = calendar.Calendar(FIRST_WEEKDAY).monthdatescalendar(month.year, month.month)
    return weeks[0][0], weeks[-1][-1]


def parse_month(text: str, default: date) -> date:
    """`YYYY-MM` from the query string, or the default; anything else is the
    default too — a typo in the address bar is not worth an error page."""
    try:
        parsed = datetime.strptime(text, "%Y-%m")
    except ValueError:
        return default.replace(day=1)
    return parsed.date().replace(day=1)


def shift_month(month: date, delta: int) -> date:
    index = month.year * 12 + (month.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def view_switch(current: str, hrefs: dict[str, str]) -> str:
    links = "".join(
        f'<a href="{html.escape(hrefs[key])}"'
        + (' aria-current="page"' if key == current else "")
        + f">{label}</a>"
        for key, label in VIEWS
    )
    return f'<nav class="vdb-seg" aria-label="Calendar view">{links}</nav>'


def _entry_html(entry: CalendarEntry, tz: ZoneInfo) -> str:
    event = entry.event
    local = event.starts_at.astimezone(tz)
    title = html.escape(event.title)
    if entry.visible:
        title = f'<a href="/events/{event.id}">{title}</a>'
    else:
        title = f'<span class="vdb-cal-title">{title}</span>'
    meta = entry.slot_name or entry.path
    return (
        f'<li><time datetime="{local.isoformat(timespec="minutes")}">{local:%H:%M}</time> '
        f'{title}<span class="vdb-cal-meta">{html.escape(meta)}</span></li>'
    )


def month_grid(
    entries: list[CalendarEntry],
    month: date,
    *,
    today: date,
    tz: ZoneInfo,
    prev_href: str,
    next_href: str,
    empty_note: str,
) -> str:
    """One month as a table: weekday headers, a cell per day carrying its
    number as a <time> and the day's entries as a list."""
    by_day: dict[date, list[CalendarEntry]] = {}
    for entry in entries:
        by_day.setdefault(entry.event.starts_at.astimezone(tz).date(), []).append(entry)

    label = f"{calendar.month_name[month.month]} {month.year}"
    prev_month, next_month = shift_month(month, -1), shift_month(month, 1)
    nav = (
        '<nav class="vdb-cal-nav" aria-label="Month">'
        f'<a href="{html.escape(prev_href)}" rel="prev">&larr; {calendar.month_name[prev_month.month]}</a>'
        f'<span class="vdb-cal-month">{label}</span>'
        f'<a href="{html.escape(next_href)}" rel="next">{calendar.month_name[next_month.month]} &rarr;</a>'
        "</nav>"
    )
    head = "".join(
        f'<th scope="col"><abbr title="{name}">{name[:3]}</abbr></th>'
        for name in DAY_NAMES
    )
    rows: list[str] = []
    for week in calendar.Calendar(FIRST_WEEKDAY).monthdatescalendar(
        month.year, month.month
    ):
        cells: list[str] = []
        for day in week:
            items = by_day.get(day, [])
            classes = ["vdb-cal-day"]
            if day.month != month.month:
                classes.append("vdb-cal-out")
            if not items:
                classes.append("vdb-cal-empty")
            current = ' aria-current="date"' if day == today else ""
            body = "".join(_entry_html(e, tz) for e in items)
            cells.append(
                f'<td class="{" ".join(classes)}"{current}>'
                f'<time datetime="{day.isoformat()}">'
                f'<span class="vdb-cal-dow">{DAY_NAMES[(day.weekday() + 1) % 7][:3]}</span>'
                f"{day.day}</time>" + (f"<ul>{body}</ul>" if body else "") + "</td>"
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    note = "" if entries else f'<p class="vdb-cal-note">{html.escape(empty_note)}</p>'
    return (
        f'<div class="vdb-cal-wrap">{nav}'
        f'<table class="vdb-cal"><caption class="vdb-sr-only">{label}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>{note}</div>"
    )


def window(month: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The [from, to) instants the grid for `month` needs, in the parish zone."""
    first, last = grid_bounds(month)
    return (
        datetime.combine(first, datetime.min.time(), tz),
        datetime.combine(last + timedelta(days=1), datetime.min.time(), tz),
    )
