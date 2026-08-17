"""Who has a VolunteerDB sign-in, and when they last used it.

Rendered on the volunteer profile and in the team roster, for every viewer who
can already see the person there — not gated to admins the way the account
page at /admin/users is. Two questions this answers on the spot: can this name
be reached through the app at all, and is the account one that was handed out
and never touched? Both are things a leader needs before deciding to email a
roster rather than phone it.

Nothing here changes an account; it only reports one.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from ..config import settings
from ..models import AppUser


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


def roster_account(account: AppUser | None) -> None:
    """The roster's account column: a badge for whether they can sign in, then
    the day they last did. Deliberately carries no email address — the roster
    hides those from viewers without full-roster rights, and this column is
    shown to every member."""
    if account is None:
        ui.badge("no account", color="grey").props("outline").tooltip(
            "Not registered on VolunteerDB — they cannot sign in."
        )
    elif not account.is_active:
        ui.badge("disabled", color="grey").tooltip(
            "Registered, but the account has been switched off."
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
