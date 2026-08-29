"""Who has a VolunteerDB sign-in, and when they last used it.

Rendered on the volunteer profile and in the team roster, for every viewer who
can already see the person there — not gated to admins the way the account
page at /admin/users is. Two questions this answers on the spot: can this name
be reached through the app at all, and is the account one that was handed out
and never touched? Both are things a leader needs before deciding to email a
roster rather than phone it.

Reporting only, with one seam: ``roster_account`` takes an optional ``action``
that replaces the badge for viewers allowed to *do* something about a missing
account (see ui/invites.py). The layout — including the width that keeps the
column aligned when there is nothing to say — stays here, so the two surfaces
cannot drift apart.
"""

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from ..config import settings
from ..env import current as current_env
from ..models import AppUser
from ..services import users as user_service


def _local(ts: datetime) -> datetime:
    return ts.astimezone(ZoneInfo(settings().timezone))


def last_login_text(account: AppUser | None) -> str:
    """Profile-line value: the date, or the reason there isn't one.

    A disabled account keeps its date and says so — "disabled" alone would
    hide the fact that somebody did once sign in with it."""
    if account is None:
        return "no VolunteerDB account"
    when = (
        f"{_local(account.last_login_at):%Y-%m-%d %H:%M %Z}"
        if account.last_login_at is not None
        else "never signed in"
    )
    return when if account.is_active else f"{when} — account disabled"


def invitable(account: AppUser | None) -> bool:
    """Whether an account-creation link would mean anything for this person.

    True when there is no account at all, or one nobody has ever used: no
    password set and never signed in. A switched-off account is excluded —
    re-arming that is an admin's call. These are exactly the guards in
    services/users.invite_volunteer, so the control is never offered for
    something the service would then refuse."""
    if account is None:
        return True
    return (
        account.is_active
        and account.password_hash is None
        and account.last_login_at is None
    )


def roster_account(
    account: AppUser | None, *, action: Callable[[], None] | None = None
) -> None:
    """The roster's account column: a badge for whether they can sign in, then
    the day they last did. Deliberately carries no email address — the roster
    hides those from viewers without full-roster rights, and this column is
    shown to every member.

    `action` renders the invite control in place of the badge, for a viewer who
    may send one and a state where sending makes sense. The precedence below
    matches the admin page's chain (admin_page.py), so the two never disagree
    about what an account is doing.
    """
    # Both invite states describe somebody who has not arrived yet. Without the
    # never-signed-in gate, an admin's password reset (reissue_invite arms a
    # link on a live account) would make the roster say an established member
    # was still waiting to set up.
    unused = account is not None and account.is_active and account.last_login_at is None
    outstanding = unused and user_service.invite_live(
        account, now=current_env().clock.now()
    )
    lapsed = unused and bool(account.invite_token) and not outstanding
    if action is not None and invitable(account):
        action()
    elif account is None:
        ui.badge("no account", color="muted").props("outline").tooltip(
            "Not registered on VolunteerDB — they cannot sign in."
        )
    elif not account.is_active:
        ui.badge("disabled", color="muted").tooltip(
            "Registered, but the account has been switched off."
        )
    elif outstanding:
        # What the data records is that a link is *outstanding* — /admin/users
        # can hand one out without mailing it — so the precise truth goes in the
        # tooltip while the badge says the thing a leader just did.
        until = account.invite_expires_at
        ui.badge("invite sent", color="warning").tooltip(
            f"An invite link is outstanding — it works until {_local(until):%Y-%m-%d}."
            if until
            else "An invite link is outstanding."
        )
    elif lapsed:
        ui.badge("invite expired", color="muted").tooltip(
            "The invite link ran out unused. They can still sign in with an "
            "emailed code."
        )
    else:
        ui.badge("account", color="positive").props("outline").tooltip(
            "Registered on VolunteerDB and able to sign in."
        )

    label = ui.label().classes("text-xs text-gray-500 w-36")
    if account is None:
        return  # keep the empty column so the rows below still line up
    if account.last_login_at is None:
        label.text = "never signed in"
        label.tooltip("The account exists but has not been used yet.")
    else:
        local = _local(account.last_login_at)
        label.text = f"last login {local:%Y-%m-%d}"
        label.tooltip(f"{local:%Y-%m-%d %H:%M %Z}")
