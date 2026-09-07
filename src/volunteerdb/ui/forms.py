"""The shapes every dialog on the site takes.

A dialog here is a card with a title, some fields, and a row of buttons on
the right: Cancel, and the one thing the dialog does. A confirmation is the
same card with a question in it and two answers. These helpers draw those
shapes so a page only writes what is particular to its own dialog -- the
fields and the command -- and every dialog reads and behaves alike.

Two widths and no more: the narrow card for a question or a short form,
the wide one for a form with a paragraph in it. Every ``ui.dialog()`` in
ui/ is built here (tests/test_ui_layer.py holds that), which is what makes
the two decisions below site-wide.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from nicegui import ui
from nicegui.elements.mixins.validation_element import ValidationElement

from .widgets import busy

NARROW = "w-96"
WIDE = "w-[32rem]"
REQUIRED = "Required"


def _present(value: Any) -> bool:
    """A value the reader gave: not None, not blank, not the 0 a picker
    uses for its "— choose —" line."""
    if value is None or value == 0:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    return True


def required[F: ValidationElement](field: F) -> F:
    """Mark a field required: " *" on its label, and a rule that refuses a
    blank -- shown under the field as "Required" the moment it is left
    empty, and again by `valid()` before the command runs. Comes first in
    the field's rules, so a blank date says Required, not the format rule.

        name = required(ui.input("Name")).props("outlined dense")
    """
    label = field.props.get("label")
    if label:
        field.props(f'label="{label} *"')
    rules: dict[str, Callable[[Any], bool]] = {REQUIRED: _present}
    current = field.validation
    if isinstance(current, dict):
        rules.update(cast(dict[str, Callable[[Any], bool]], current))
    field.validation = rules
    # the setter validates at once, which would open every form with
    # "Required" under its blank fields: the rule speaks when the reader
    # leaves the field empty, or on submit, not before they have begun
    field.error = None
    return field


def valid(*fields: ValidationElement | None) -> bool:
    """Every field's rules, run before the command; the first field that
    fails gets the focus, so the reader lands on what to fix. A None is a
    field the dialog does not have (a current password on a code sign-in)."""
    present = [f for f in fields if f is not None]
    failing = [f for f in present if not f.validate()]
    if failing:
        failing[0].run_method("focus")
        return False
    return True


@contextmanager
def dialog_card(
    title: str, *, width: str = NARROW, persistent: bool = True
) -> Iterator[ui.dialog]:
    """A dialog around a titled card. The body is built inside the block;
    the caller opens the dialog (`dialog.open()`) once it is built, or
    awaits it for an answer (see `confirm`).

    Persistent by default: a dialog with fields in it does not vanish on a
    click beside it or on Escape, because that click was as likely a slip
    as a decision, and the half-typed form went with it. Cancel is on every
    button row (`actions`), so closing is one deliberate click away."""
    dialog = ui.dialog()
    if persistent:
        dialog.props("persistent")
    with dialog, ui.card().classes(f"{width} gap-3"):
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
    cancel: str | None = "Cancel",
    on_cancel: Callable[..., Any] | None = None,
    extra: Callable[[], Any] | None = None,
) -> ui.button:
    """The button row: Cancel closes, `primary` does the thing. `danger`
    paints the primary as destructive; `marker` names it for the tests.

    `cancel` renames the way out (a dialog that is awaited answers False
    through `on_cancel`), or None drops it for a dialog that only informs.
    `extra` draws whatever sits between the two -- a Clear, a Remove, a
    Copy -- so every row still reads left to right: the way out, the side
    doors, the deed. The primary is `busy` while it works (widgets.busy)."""
    with ui.row().classes("justify-end w-full gap-2"):
        if cancel is not None:
            ui.button(cancel, on_click=on_cancel or dialog.close).props("flat")
        if extra is not None:
            extra()
        button = ui.button(primary, icon=icon, on_click=busy(on_primary))
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
    yes_marker: str = "confirm-yes",
    no_marker: str = "confirm-no",
    width: str = NARROW,
) -> bool:
    """Ask before acting: True when the reader chose `yes`. `detail` is a
    quieter second line for what the action entails.

    Every action that removes a record or takes a person off something comes
    through here first (tests/test_ui_layer.py holds the GUI to it). The
    wording rule: the question names the object, `yes` names the verb and
    the object again ("Delete the team", "Remove Maria Alvarez from
    Hospitality"), and `no` is "Cancel" -- or "Keep it" where the action is
    itself a cancellation. The two markers are the same on every question,
    so a test can answer any of them.

    Not persistent, unlike a form: there is nothing typed to lose, and a
    click beside the question is the same answer as Cancel."""
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
    return bool(await answered(dialog))


async def answered(dialog: ui.dialog) -> Any:
    """Await a question and take it down once it is answered.

    An awaited dialog closes but stays in the page, so every question asked
    left one more behind -- and the simulation, which clicks the lowest-id
    match for a button's text, then answered the previous question instead
    of the one on screen. A question is built per asking; deleting it
    afterwards keeps the page the size it was."""
    result = await dialog
    dialog.delete()
    return result
