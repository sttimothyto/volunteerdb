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

from collections.abc import Callable
from datetime import date, datetime, time

from nicegui import ui
from nicegui.elements.mixins.value_element import ValueElement

from .a11y import icon_button

DATE_RULE = "Use YYYY-MM-DD"
TIME_RULE = "Use HH:MM"


def iso_date(on: date) -> str:
    """A field's value for a date: the one place the shape is spelled."""
    return on.isoformat()


def iso_time(at: datetime) -> str:
    """A field's value for a time of day (already in the parish's clock)."""
    return f"{at:%H:%M}"


def _parses(parse: Callable[[str], object]) -> Callable[[str | None], bool]:
    """A field rule: the value is blank or `parse` takes it. Blank is fine
    here (forms.required says otherwise when it must be)."""

    def rule(value: str | None) -> bool:
        if not value:
            return True
        try:
            parse(value.strip())
        except ValueError:
            return False
        return True

    return rule


_is_date = _parses(date.fromisoformat)
_is_time = _parses(time.fromisoformat)


def _picker_input(
    label: str,
    value: str,
    clearable: bool,
    *,
    rule: str,
    check: Callable[[str | None], bool],
    picker: Callable[[], ValueElement],
    icon: str,
    name: str,
) -> ui.input:
    """A ``ui.input`` whose appended button opens `picker` in a menu.

    Picking a value closes the menu, typing one never opens it
    (``no-parent-event``). A typed value that fails `check` says so under
    the field (`rule`, the format in the label's own words), before the form
    is submitted.
    """
    props = "outlined dense" + (" clearable" if clearable else "")
    with ui.input(label, value=value, validation={rule: check}).props(props) as field:
        with ui.menu().props("no-parent-event") as menu:
            picker().bind_value(field).on_value_change(menu.close)
        with field.add_slot("append"):
            # a real button, not an icon with a click handler: focusable,
            # named, and 32px to hit (WCAG 2.1.1, 4.1.2, 2.5.8)
            icon_button(icon, name, on_click=menu.open).props("flat dense round")
    return field


def date_input(label: str, *, value: str = "", clearable: bool = False) -> ui.input:
    """A ``ui.input`` whose appended calendar button opens a ``ui.date``
    picker. Returns the input so callers chain their own ``.classes(...)``."""
    return _picker_input(
        label,
        value,
        clearable,
        rule=DATE_RULE,
        check=_is_date,
        picker=ui.date,
        icon="edit_calendar",
        name="Choose a date",
    )


def time_input(label: str, *, value: str = "", clearable: bool = False) -> ui.input:
    """A ``ui.input`` whose appended clock button opens a 24-hour ``ui.time``
    picker; the value stays ``HH:MM``. Same shape as date_input."""
    return _picker_input(
        label,
        value,
        clearable,
        rule=TIME_RULE,
        check=_is_time,
        picker=lambda: ui.time().props("format24h"),
        icon="schedule",
        name="Choose a time",
    )
