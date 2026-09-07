"""Whole-page refusals: the frame a reader gets when a page is not theirs.

The services decide every operation; what a page decides is only whether
to draw itself at all. The admin pages share one answer for a reader who is
not an admin, and this is it.
"""

from ..permissions import Actor
from .layout import frame
from .widgets import denied


def deny_unless_admin(actor: Actor, title: str, *, help: str) -> bool:
    """True, with the refusal page already drawn, when `actor` is not an
    admin -- so a page reads `if deny_unless_admin(actor, "Accounts",
    help="accounts"): return`. The refusal carries the way back
    (widgets.denied) and the page's own help."""
    if actor.is_admin:
        return False
    with frame(title, actor, help=help):
        denied("Admins only.", back=("Dashboard", "/"))
    return True
