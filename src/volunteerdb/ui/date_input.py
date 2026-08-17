"""A date field with a popup calendar.

The value stays a plain ``YYYY-MM-DD`` string — exactly what the text inputs
it replaces held — so every consumer (``_parse_local``, ``date.fromisoformat``)
keeps working unchanged, and the field still accepts a typed date. The
calendar is a convenience layered on top, not a new value format.
"""

from nicegui import ui


def date_input(label: str, *, value: str = "", clearable: bool = False) -> ui.input:
    """A ``ui.input`` whose appended calendar icon opens a ``ui.date`` picker.

    Returns the input so callers chain their own ``.classes(...)``; picking a
    date closes the menu, typing one never opens it (``no-parent-event``).
    """
    props = "outlined dense" + (" clearable" if clearable else "")
    with ui.input(label, value=value).props(props) as field:
        with ui.menu().props("no-parent-event") as menu:
            ui.date().bind_value(field).on_value_change(menu.close)
        with field.add_slot("append"):
            ui.icon("edit_calendar").on("click", menu.open).classes("cursor-pointer")
    return field
