"""A date field with a popup calendar.

The value stays a plain ``YYYY-MM-DD`` string — exactly what the text inputs
it replaces held — so every consumer (``_parse_local``, ``date.fromisoformat``)
keeps working unchanged, and the field still accepts a typed date. The
calendar is a convenience layered on top, not a new value format.
"""

from nicegui import ui

from .a11y import icon_button


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
            # a real button, not an icon with a click handler: focusable,
            # named, and 32px to hit (WCAG 2.1.1, 4.1.2, 2.5.8)
            icon_button("edit_calendar", "Choose a date", on_click=menu.open).props(
                "flat dense round"
            )
    return field
