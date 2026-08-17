"""Statistic tiles for the dashboard's three sections.

Plain labels inside a themed card — no chart library, no canvas. The last
time this page was profiled the finding was that browser JS, not the
database, was what made it slow, so a row of numbers must not arrive with a
renderer attached.

Every tile that stands for a set of things takes an `href` and becomes a real
<a>, the way the header's nav buttons do: a number you cannot click is a
number you have to go looking for.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from nicegui import ui


@contextmanager
def stat_section(title: str, subtitle: str | None = None) -> Iterator[None]:
    """A titled band. Heading matches the section headings on the events and
    elections pages. The body is a column, so a section can hold a row of
    tiles and then a line of chips or links under it."""
    ui.label(title).classes("text-lg font-medium mt-2")
    if subtitle:
        ui.label(subtitle).classes("text-sm text-gray-500 vdb-prose")
    with ui.column().classes("w-full gap-2"):
        yield


@contextmanager
def tile_row() -> Iterator[None]:
    """Tiles side by side, wrapping on a narrow window. items-stretch so a
    tile with a sub-line does not make its neighbours short."""
    with ui.row().classes("items-stretch gap-3 flex-wrap w-full"):
        yield


@contextmanager
def chip_row(label: str) -> Iterator[None]:
    """A labelled line of chips under a tile row — for a set that comes in
    bands or phases and reads better as small facts than as one figure."""
    with ui.row().classes("items-center gap-2 flex-wrap"):
        ui.label(label).classes("text-sm text-gray-500")
        yield


def stat_tile(
    value: str | int,
    caption: str,
    *,
    sub: str | None = None,
    href: str | None = None,
    hint: str | None = None,
    warn: bool = False,
) -> None:
    """One number and what it counts. `warn` tints a figure that wants doing
    something about — a leaderless team, an unfilled shift — rather than one
    that is merely large."""
    with ExitStack() as stack:
        if href is not None:
            # a real anchor, so right-click / middle-click open a new tab
            stack.enter_context(ui.link(target=href).classes("vdb-quiet"))
        classes = "vdb-stat" + (" vdb-stat-warn" if warn else "")
        stack.enter_context(ui.card().classes(classes))
        if hint:
            ui.tooltip(hint)
        ui.label(str(value)).classes("vdb-stat-value")
        ui.label(caption).classes("vdb-stat-caption")
        # the sub-line is always rendered so tiles in a row keep one baseline
        ui.label(sub or "").classes("vdb-stat-sub")


def stat_chip(
    label: str, count: int, *, color: str | None = None, href: str | None = None
) -> None:
    """A swatch, a name and a count — for a set that comes in bands or phases
    and reads better as a row of small facts than as one big number. The
    swatch reuses the graph legend's dot so the workload colours mean the
    same thing in both places on this page."""
    with ExitStack() as stack:
        if href is not None:
            stack.enter_context(ui.link(target=href).classes("vdb-quiet"))
        stack.enter_context(ui.row().classes("items-center gap-2 vdb-stat-chip"))
        if color is not None:
            ui.element("span").classes("vdb-legend-swatch vdb-legend-dot").style(
                f"background: {color}"
            )
        ui.label(label).classes("text-sm")
        ui.label(str(count)).classes("text-sm font-medium")
