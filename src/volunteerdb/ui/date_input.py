"""Date and time fields with a popup picker.

The value stays a plain ``YYYY-MM-DD`` (or ``HH:MM``) string — exactly what
the text inputs they replace held — so every consumer (``_parse_local``,
``date.fromisoformat``, the custom-field codec) keeps working unchanged, and
the field still accepts a typed value. The picker is a convenience layered on
top, not a new value format. Every date or time a form asks for goes through
here (tests/test_ui_css_invariants.py checks), so no field is left to typing
alone: the Quasar picker has one keyboard model and one look in both modes,
where a native ``<input type=date>`` has one per browser.
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


def time_input(label: str, *, value: str = "", clearable: bool = False) -> ui.input:
    """A ``ui.input`` whose appended clock button opens a 24-hour ``ui.time``
    picker; the value stays ``HH:MM``. Same shape as date_input."""
    props = "outlined dense" + (" clearable" if clearable else "")
    with ui.input(label, value=value).props(props) as field:
        with ui.menu().props("no-parent-event") as menu:
            ui.time().props("format24h").bind_value(field).on_value_change(menu.close)
        with field.add_slot("append"):
            icon_button("schedule", "Choose a time", on_click=menu.open).props(
                "flat dense round"
            )
    return field
