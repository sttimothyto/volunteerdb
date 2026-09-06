"""The shapes every dialog on the site takes.

A dialog here is a card with a title, some fields, and a row of buttons on
the right: Cancel, and the one thing the dialog does. A confirmation is the
same card with a question in it and two answers. These helpers draw those
shapes so a page only writes what is particular to its own dialog -- the
fields and the command -- and every dialog reads and behaves alike.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from nicegui import ui


@contextmanager
def dialog_card(title: str, *, width: str = "w-96") -> Iterator[ui.dialog]:
    """A dialog around a titled card. The body is built inside the block;
    the caller opens the dialog (`dialog.open()`) once it is built, or
    awaits it for an answer (see `confirm`)."""
    with ui.dialog() as dialog, ui.card().classes(f"{width} gap-3"):
        ui.label(title).classes("text-lg font-medium")
        yield dialog


def actions(
    dialog: ui.dialog,
    primary: str,
    on_primary: Callable[..., Any],
    *,
    icon: str | None = None,
    danger: bool = False,
    marker: str | None = None,
    cancel: str = "Cancel",
) -> ui.button:
    """The button row: Cancel closes, `primary` does the thing. `danger`
    paints the primary as destructive; `marker` names it for the tests."""
    with ui.row().classes("justify-end w-full gap-2"):
        ui.button(cancel, on_click=dialog.close).props("flat")
        button = ui.button(primary, icon=icon, on_click=on_primary)
        if danger:
            button.props("color=negative")
        if marker:
            button.mark(marker)
    return button


async def confirm(
    question: str,
    *,
    yes: str,
    no: str = "Cancel",
    detail: str | None = None,
    icon: str | None = None,
    danger: bool = False,
    yes_marker: str | None = None,
    no_marker: str | None = None,
    width: str = "w-96",
) -> bool:
    """Ask before acting: True when the reader chose `yes`. `detail` is a
    quieter second line for what the action entails."""
    with ui.dialog() as dialog, ui.card().classes(f"{width} gap-3"):
        ui.label(question).classes("font-medium")
        if detail:
            ui.label(detail).classes("text-sm text-gray-500")
        with ui.row().classes("justify-end w-full gap-2"):
            no_button = ui.button(no, on_click=lambda: dialog.submit(False)).props(
                "flat"
            )
            if no_marker:
                no_button.mark(no_marker)
            yes_button = ui.button(yes, icon=icon, on_click=lambda: dialog.submit(True))
            if danger:
                yes_button.props("color=negative")
            if yes_marker:
                yes_button.mark(yes_marker)
    return bool(await dialog)
