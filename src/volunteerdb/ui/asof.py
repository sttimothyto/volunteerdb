"""Time travel on a page: the `?as_of=` parameter, and the strip and picker
that show and change it.

Every page that can be read as of a past instant does three things with it:
parses the query string (`parse_as_of`), shows a banner saying the page is a
snapshot (`asof_banner`, drawn by layout.frame), and offers a date picker in
the header's settings menu (`asof_picker`). They live together here because
they are one feature; the request context (ui/context.py) is where the
parsed instant rides once a page has it.
"""

from datetime import datetime, time, timedelta, tzinfo
from urllib.parse import quote

from nicegui import ui

from .. import asof_param
from ..env import current as current_env

# The pages that can be read as of a past instant. The header carries the
# date on their links while a snapshot is open; every other page shows today,
# and the banner says so.
SNAPSHOT_PATHS = ("/", "/teams")
SHOW_TODAY = "Volunteers, Events and Elections show today."


def as_of_query(as_of: datetime) -> str:
    """The `?as_of=` value that reads back as `as_of`: a bare date where the
    instant is the end of a day in its own zone (which is what a typed date
    means: parse_as_of reads it in the parish's), the full timestamp
    otherwise, URL-safe either way. No Env read: the instant carries its
    zone, and the page's own context is the only one a frame reaches."""
    zone: tzinfo | None = as_of.tzinfo
    end_of_day = (
        datetime.combine(as_of.date(), time.min, zone)
        + timedelta(days=1)
        - timedelta(microseconds=1)
    )
    if as_of == end_of_day:
        return as_of.date().isoformat()
    return quote(as_of.isoformat(), safe="")


def with_as_of(href: str, query: str) -> str:
    """`href` carrying the snapshot, if it is a page that can show one."""
    if query and href in SNAPSHOT_PATHS:
        return f"{href}?as_of={query}"
    return href


def parse_as_of(raw: str, tz: tzinfo) -> datetime | None:
    """Query-param 'as of': a date means end of that day, parish time (`tz` is
    the Env's). Shared with the API (see asof_param.py); a page ignores garbage
    and renders live data rather than erroring at the reader."""
    try:
        return asof_param.parse_as_of(raw, tz)
    except ValueError:
        return None


def asof_banner(as_of: datetime, base_path: str) -> None:
    """The 'you are reading history' strip, carrying its own way back.

    Rendered by frame() in the page body: the picker hides in the header's
    settings menu, but a snapshot must never be silent -- nor end silently:
    the line says which pages show today, since the header's links to them
    drop the date."""
    with ui.row().classes("w-full bg-amber-100 rounded p-2 items-center gap-2"):
        ui.icon("history")
        with ui.column().classes("gap-0"):
            ui.label(
                f"Read-only snapshot as of {as_of.astimezone(current_env().tz).strftime('%Y-%m-%d %H:%M %Z')}"
            ).classes("text-amber-900 font-medium")
            ui.label(SHOW_TODAY).classes("text-sm text-amber-800")
        ui.space()
        ui.button("Back to now").props(f'dense color=warning href="{base_path}"')


def asof_picker(as_of: datetime | None, base_path: str) -> None:
    """Date picker for the header settings menu; clearing it returns to now."""
    from .date_input import date_input  # date_input imports a11y, which is ui-only

    field = date_input(
        "View as of (YYYY-MM-DD)",
        value=as_of.date().isoformat() if as_of is not None else "",
        clearable=True,
    ).classes("w-full")

    def go() -> None:
        value = (field.value or "").strip()
        ui.navigate.to(f"{base_path}?as_of={value}" if value else base_path)

    field.on("keydown.enter", go)
    ui.button("View", on_click=go).props("dense outline")
