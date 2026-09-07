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

from datetime import date, datetime, time

from nicegui import ui

from .a11y import icon_button

DATE_RULE = "Use YYYY-MM-DD"
TIME_RULE = "Use HH:MM"


def iso_date(on: date) -> str:
    """A field's value for a date: the one place the shape is spelled."""
    return on.isoformat()


def iso_time(at: datetime) -> str:
    """A field's value for a time of day (already in the parish's clock)."""
    return f"{at:%H:%M}"


def _is_date(value: str | None) -> bool:
    """Blank is fine here (forms.required says otherwise when it must be)."""
    if not value:
        return True
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _is_time(value: str | None) -> bool:
    if not value:
        return True
    try:
        time.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def date_input(label: str, *, value: str = "", clearable: bool = False) -> ui.input:
    """A ``ui.input`` whose appended calendar icon opens a ``ui.date`` picker.

    Returns the input so callers chain their own ``.classes(...)``; picking a
    date closes the menu, typing one never opens it (``no-parent-event``).
    A typed value that is not a date says so under the field (the rule is
    the label's own words), before the form is submitted.
    """
    props = "outlined dense" + (" clearable" if clearable else "")
    with ui.input(label, value=value, validation={DATE_RULE: _is_date}).props(
        props
    ) as field:
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
    with ui.input(label, value=value, validation={TIME_RULE: _is_time}).props(
        props
    ) as field:
        with ui.menu().props("no-parent-event") as menu:
            ui.time().props("format24h").bind_value(field).on_value_change(menu.close)
        with field.add_slot("append"):
            icon_button("schedule", "Choose a time", on_click=menu.open).props(
                "flat dense round"
            )
    return field
