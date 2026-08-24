"""The one helper that keeps icon-only controls announceable.

Quasar renders a material icon as an <i> whose text is the ligature and marks
it aria-hidden, and a .tooltip() beside it is a QTooltip, not a name — so a
bare ui.button(icon=...) is read out as "button". icon_button() gives the
control the same words as its tooltip, once, in the place a screen reader
looks (WCAG 4.1.2). Everything else about it is an ordinary QBtn.
"""

import html

from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement


def icon_button(icon: str, label: str, **kwargs) -> ui.button:
    """A ui.button(icon=...) whose accessible name is `label`, which also
    serves as its tooltip. Extra keyword arguments go to ui.button."""
    button = ui.button(icon=icon, **kwargs).props(
        f'aria-label="{html.escape(label, quote=True)}"'
    )
    button.tooltip(label)
    return button


class Heading(TextElement):
    """A real <h1>…<h6>. ui.label renders a <div>, which is fine for a
    caption and wrong for the one line that names the page: a screen reader
    lists headings to get around, and a page with none has no map (2.4.6,
    and axe's page-has-heading-one). Stays a TextElement, so tests find it
    by its text exactly as they found the label."""

    def __init__(self, text: str, *, level: int = 1) -> None:
        super().__init__(tag=f"h{level}", text=text)


def heading(text: str, *, level: int = 1) -> Heading:
    return Heading(text, level=level)
