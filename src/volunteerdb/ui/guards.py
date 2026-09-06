"""Whole-page refusals: the frame a reader gets when a page is not theirs.

The services decide every operation; what a page decides is only whether
to draw itself at all. The admin pages share one answer for a reader who is
not an admin, and this is it.
"""

from nicegui import ui

from ..permissions import Actor
from .layout import frame


def deny_unless_admin(actor: Actor, title: str) -> bool:
    """True, with the refusal page already drawn, when `actor` is not an
    admin -- so a page reads `if deny_unless_admin(actor, "Accounts"): return`."""
    if actor.is_admin:
        return False
    with frame(title, actor):
        ui.label("Admins only.").classes("text-gray-500")
    return True
