"""Who has a VolunteerDB sign-in, and when they last used it.

Rendered on the volunteer profile and in the team roster, for every viewer who
can already see the person there — not gated to admins the way the account
page at /admin/users is. Two questions this answers on the spot: can this name
be reached through the app at all, and is the account one that was handed out
and never touched? Both are things a leader needs before deciding to email a
roster rather than phone it.

Reporting only. ``account_state`` is the roster's answer as a value -- the
badge's words, colour and tooltip, the last-login line -- which the roster
table carries in its rows; the profile page draws ``last_login_text``. The
invite control that sits beside them is ui/invites.py's.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import timefmt
from ..env import current as current_env
from ..models import AppUser
from ..services import users as user_service


def _words(ts: datetime) -> str:
    """'Thu, Sep 10, 7:30 PM' in the parish's clock."""
    return timefmt.when_short(ts, current_env().tz)


def last_login_text(account: AppUser | None) -> str:
    """Profile-line value: the date, or the reason there isn't one.

    A disabled account keeps its date and says so — "disabled" alone would
    hide the fact that somebody did once sign in with it."""
    if account is None:
        return "no VolunteerDB account"
    when = (
        _words(account.last_login_at)
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


@dataclass(frozen=True)
class AccountState:
    """The roster's account column as a value: the badge and the line
    beside it. Deliberately carries no email address -- the roster hides
    those from viewers without full-roster rights, and this column is shown
    to every member."""

    label: str  # no account / disabled / invite sent / invite expired / account
    color: str
    outline: bool
    tooltip: str
    last_login: str  # "never signed in", "last login Mar 1, 2026", or ""
    last_login_tooltip: str


def account_state(
    account: AppUser | None, *, now: datetime, tz: ZoneInfo
) -> AccountState:
    """Whether they can sign in, and when they last did.

    The precedence matches the admin page's chain (admin_page.py), so the
    two never disagree about what an account is doing. Both invite states
    describe somebody who has not arrived yet: without the never-signed-in
    gate, an admin's password reset (reissue_invite arms a link on a live
    account) would make the roster say an established member was still
    waiting to set up."""
    if account is None:
        return AccountState(
            "no account",
            "muted",
            True,
            "Not registered on VolunteerDB — they cannot sign in.",
            "",
            "",
        )
    if account.last_login_at is None:
        last, last_tip = (
            "never signed in",
            "The account exists but has not been used yet.",
        )
    else:
        last = f"last login {timefmt.day(account.last_login_at, tz)}"
        last_tip = timefmt.when_short(account.last_login_at, tz)
    unused = account.is_active and account.last_login_at is None
    outstanding = unused and user_service.invite_live(account, now=now)
    lapsed = unused and bool(account.invite_token) and not outstanding
    if not account.is_active:
        return AccountState(
            "disabled",
            "muted",
            False,
            "Registered, but the account has been switched off.",
            last,
            last_tip,
        )
    if outstanding:
        # What the data records is that a link is *outstanding* -- /admin/users
        # can hand one out without mailing it -- so the precise truth goes in
        # the tooltip while the badge says the thing a leader just did.
        until = account.invite_expires_at
        return AccountState(
            "invite sent",
            "warning",
            False,
            f"An invite link is outstanding — it works until "
            f"{timefmt.when_short(until, tz)}."
            if until
            else "An invite link is outstanding.",
            last,
            last_tip,
        )
    if lapsed:
        return AccountState(
            "invite expired",
            "muted",
            False,
            "The invite link ran out unused. They can still sign in with an "
            "emailed code.",
            last,
            last_tip,
        )
    return AccountState(
        "account",
        "positive",
        True,
        "Registered on VolunteerDB and able to sign in.",
        last,
        last_tip,
    )
