"""Accounts: who may sign in, and how.

Every function that can refuse returns a Result; every moment, token and
code arrives as a parameter (the edge reads its clock and its random source
once), and the password policy's site terms come the same way. An `Invite`
is what arming a sign-in link needs from the edge -- the plaintext token, the
moment, and how long it lives -- because only the token's digest is stored
and the plaintext exists nowhere else.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from pydantic import validate_email
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    async_burn_password_check,
    async_hash_password,
    async_verify_password,
    needs_rehash,
)
from ..domain import (
    EmailChangeCancelled,
    EmailChanged,
    EmailChangeRequested,
    InviteIssued,
    InviteRedeemed,
    OtpIssued,
    Outcome,
    PasswordChanged,
)
from ..errors import (
    BadCredentials,
    DomainError,
    Invalid,
    NotFound,
    invalid,
    not_found,
    require,
)
from ..fp import Err, Ok, Result
from ..models import AppUser, Volunteer
from ..passwords import check as check_password
from ..permissions import Actor
from . import volunteers as volunteer_service

OTP_TTL = timedelta(minutes=10)
OTP_RESEND_INTERVAL = timedelta(seconds=60)
OTP_MAX_ATTEMPTS = 5
# How long the link mailed to a *new* address stays usable. The 24 hours NIST
# SP 800-63B §4.2.1.2 allows a code sent to an email address, taken straight
# rather than stretched the way invite_ttl_hours is: the invite link is
# stretched to a week because a volunteer who misses it is locked out until an
# admin re-cuts it, whereas missing this one costs nothing — the account keeps
# working at its old address and they can simply ask again.
EMAIL_CHANGE_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Invite:
    """What arming an invite/reset link needs from the edge: the plaintext
    token it minted, the moment, and how long the link lives
    (VDB_INVITE_TTL_HOURS). env.Env.invite() builds one."""

    token: str
    now: datetime
    ttl: timedelta


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _invite_issued(user: AppUser, token: str, invite: Invite) -> InviteIssued:
    """The fact an armed link is: who, where it goes, and for how long."""
    return InviteIssued(
        user.id, user.email, token, int(invite.ttl.total_seconds() // 3600)
    )


def _issue_invite(user: AppUser, invite: Invite) -> str:
    """Arm the account's invite/reset link. Token and expiry are set and
    cleared as a pair, so a token with no live expiry is simply dead.

    Only the SHA-256 digest is stored, as for api_token: the link is a bearer
    credential that signs the holder in as that account, so a read of the
    database — or of a backup — must not hand out live ones. The plaintext is
    returned here and never again, which is why an outstanding invite can be
    re-sent but not re-displayed."""
    user.invite_token = _token_digest(invite.token)
    user.invite_expires_at = invite.now + invite.ttl
    return invite.token


def _clear_invite(user: AppUser) -> None:
    user.invite_token = None
    user.invite_expires_at = None


def invite_live(user: AppUser, now: datetime) -> bool:
    """Whether the account's invite link can still be redeemed."""
    return bool(
        user.invite_token and user.invite_expires_at and user.invite_expires_at > now
    )


def normalize_email(raw: str) -> Result[str, Invalid]:
    """Trim, lowercase, and refuse anything that is not an address.

    The one place that decides what an address *is*. `users.create` and the
    API's EmailStr each did half of this; a login address is compared as an
    exact string (`get_by_email`), so folding case here is what stops
    Maria@example.org and maria@example.org becoming two accounts."""
    addr = (raw or "").strip().lower()
    try:
        validate_email(addr)
    except ValueError:  # PydanticCustomError, whose message is jargon
        return invalid(f"{raw.strip()!r} is not an email address")
    return Ok(addr)


async def get(session: AsyncSession, user_id: int) -> AppUser | None:
    return await session.get(AppUser, user_id)


async def get_by_email(session: AsyncSession, email: str) -> AppUser | None:
    return (
        await session.execute(
            sa.select(AppUser).where(AppUser.email == email.strip().lower())
        )
    ).scalar_one_or_none()


def _require_admin(actor: Actor | None):
    """Account management is admin-only, and this is where that is decided.

    The one exception is invite_volunteer below, which a team's leaders,
    seconds and core members may use for one of their own people — it carries
    its own check and says why.

    `actor=None` is a trusted internal caller: the deploy bootstrap, the seed
    script, the Drive-sync bot creating its own login, and the self-service
    paths on /account, which act on the signed-in account by construction.
    """
    return require(actor is None or actor.is_admin, "manage accounts")


async def list_all(
    session: AsyncSession, actor: Actor | None = None
) -> Result[list[AppUser], DomainError]:
    """Every account. Admin-only — the list is the parish's sign-in surface."""
    if denied := _require_admin(actor):
        return denied
    return Ok(
        list(
            (
                await session.execute(sa.select(AppUser).order_by(AppUser.email))
            ).scalars()
        )
    )


async def accounts_by_volunteer(
    session: AsyncSession, volunteer_ids: list[int]
) -> dict[int, AppUser]:
    """volunteer id -> their sign-in account, for the ids that have one.

    One query for a whole roster; a missing key means that volunteer has no
    account at all. app_user.volunteer_id is unique, so there is never more
    than one to choose between."""
    if not volunteer_ids:
        return {}
    rows = await session.execute(
        sa.select(AppUser).where(AppUser.volunteer_id.in_(volunteer_ids))
    )
    return {u.volunteer_id: u for u in rows.scalars() if u.volunteer_id is not None}


async def account_for_volunteer(
    session: AsyncSession, volunteer_id: int
) -> AppUser | None:
    return (await accounts_by_volunteer(session, [volunteer_id])).get(volunteer_id)


async def authenticate(
    session: AsyncSession, email: str, password: str, *, now: datetime
) -> Result[AppUser, BadCredentials]:
    """The account behind a correct email and password, or BadCredentials --
    the same refusal whatever the reason, so nothing here is an
    account-existence oracle. Records the sign-in at `now`."""
    user = await get_by_email(session, email)
    if user is None or not user.is_active or user.password_hash is None:
        # uniform timing: no account-existence oracle
        await async_burn_password_check(password)
        return Err(BadCredentials("no password-bearing account at that address"))
    if not await async_verify_password(user.password_hash, password):
        return Err(BadCredentials("wrong password"))
    if needs_rehash(user.password_hash):
        # The cost factors went up since this password was set; sign-in is the
        # one moment the plaintext is in hand, so re-stretch it now.
        user.password_hash = await async_hash_password(password)
    user.last_login_at = now
    await session.flush()
    return Ok(user)


async def authenticate_token(session: AsyncSession, token: str) -> AppUser | None:
    if not token:
        return None
    user = (
        await session.execute(
            sa.select(AppUser).where(AppUser.api_token == _token_digest(token))
        )
    ).scalar_one_or_none()
    return user if user is not None and user.is_active else None


async def _volunteer_for_email(session: AsyncSession, email: str) -> int | None:
    """The volunteer a new account at this address should adopt, when it is
    unambiguous: exactly one active volunteer holds the address, and no other
    account is linked to them already. A family sharing an address resolves to
    no match rather than to a coin flip."""
    candidates = await volunteer_service.find_by_email(session, email)
    if len(candidates) != 1:
        return None
    taken = await session.execute(
        sa.select(AppUser.id).where(AppUser.volunteer_id == candidates[0].id)
    )
    return None if taken.first() is not None else candidates[0].id


async def create(
    session: AsyncSession,
    email: str,
    *,
    actor: Actor | None = None,
    volunteer_id: int | None = None,
    is_admin: bool = False,
    password: str | None = None,
    link_by_email: bool = True,
    invite: Invite | None = None,
    site_terms: frozenset[str] = frozenset(),
) -> Result[tuple[AppUser, str | None], DomainError]:
    """Create an account, and hand back the invite link it armed.

    Returns (account, plaintext invite token) — the token is None when a
    password was supplied, since then there is no link. Without a password
    the account gets the time-limited invite link the edge minted (`invite`
    is then required), redeemed via the invite link (emailed, or handed out).

    A password given here is held to the same policy as one the volunteer
    chooses for themselves (passwords.check), so no path into the database can
    leave a weak password behind — Err(WeakPassword) if it fails.

    With no volunteer_id given, the account adopts the volunteer holding the
    same email address when exactly one does — an account exists to be some
    volunteer's login, and an unlinked one sees an empty app. Pass
    link_by_email=False for accounts that are not a person (the sync bot).
    """
    if denied := _require_admin(actor):
        return denied
    addr = email.strip().lower()
    if password:
        if weak := check_password(password, email=addr, site_terms=site_terms):
            return weak
    else:
        assert invite is not None, "an account without a password needs an invite"
    if volunteer_id is None and link_by_email:
        volunteer_id = await _volunteer_for_email(session, addr)
    password_hash = await async_hash_password(password) if password else None
    user = AppUser(
        email=addr,
        volunteer_id=volunteer_id,
        is_admin=is_admin,
        password_hash=password_hash,
    )
    token = None if password else _issue_invite(user, invite)  # type: ignore[arg-type]
    session.add(user)
    await session.flush()
    return Ok((user, token))


async def set_password(
    session: AsyncSession,
    user_id: int,
    password: str,
    *,
    site_terms: frozenset[str] = frozenset(),
) -> Result[Outcome[None], DomainError]:
    """Bind a password to an account (WeakPassword if it fails the policy).
    The PasswordChanged event is the account's notice -- mailed and logged by
    the policy, on a channel the session making the change cannot suppress.

    Also spends any outstanding invite link: whoever set this password is in
    possession of the account, and a reset link left armed behind them would
    be a second key to it."""
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    if weak := check_password(password, email=user.email, site_terms=site_terms):
        return weak
    user.password_hash = await async_hash_password(password)
    _clear_invite(user)
    await session.flush()
    return Ok(Outcome(None, (PasswordChanged(user.id, user.email, removed=False),)))


async def clear_password(
    session: AsyncSession, user_id: int
) -> Result[Outcome[None], DomainError]:
    """Drop back to email-code-only sign-in. The API token goes with it —
    tokens are issued against a password, so keeping one alive would leave a
    credential the account can no longer re-issue or reason about."""
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    user.password_hash = None
    user.api_token = None
    await session.flush()
    return Ok(Outcome(None, (PasswordChanged(user.id, user.email, removed=True),)))


async def reissue_invite(
    session: AsyncSession,
    user_id: int,
    *,
    invite: Invite,
    actor: Actor | None = None,
) -> Result[Outcome[str], DomainError]:
    """New invite link for a user who lost their password. Invalidates the old
    password — this is also how an admin forces a change on an account believed
    to be compromised (NIST SP 800-63B §3.1.1.2).

    The API token goes with it, for the same reason clear_password drops it:
    it was issued against the password being invalidated. Leaving it alive
    meant the one action an admin takes against a compromised account left the
    stolen credential working."""
    if denied := _require_admin(actor):
        return denied
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    token = _issue_invite(user, invite)
    user.password_hash = None
    user.api_token = None
    await session.flush()
    return Ok(Outcome(token, (_invite_issued(user, token, invite),)))


async def invite_volunteer(
    session: AsyncSession, volunteer_id: int, *, invite: Invite
) -> Result[Outcome[tuple[AppUser, str]], DomainError]:
    """Give one volunteer the sign-in account they don't have yet, with its
    invite link armed. Returns (account, token); the InviteIssued event is
    what the policy mails to the address on the volunteer's own record.

    This is the narrow, team-scoped counterpart to bulk_provision: it is what a
    ministry leader triggers from their own roster, so it refuses anything that
    would touch an account they don't plainly own the creation of. Permission is
    the caller's job (permissions.Actor.can_invite_volunteer).

    Refusals are Invalid (422 / a warning toast) with a sentence the leader
    can act on, because every one of them has a different remedy.
    """
    volunteer = await volunteer_service.get(session, volunteer_id)
    if volunteer is None:
        return not_found("volunteer", volunteer_id)
    if not volunteer.is_active:
        return invalid(
            f"{volunteer.full_name} is archived — reactivate them before inviting"
        )
    addr = (volunteer.email or "").strip().lower()
    if not addr:
        return invalid(
            f"{volunteer.full_name} has no email address on file — add one first"
        )

    account = await account_for_volunteer(session, volunteer_id)
    if account is None:
        # An account may already sit at this address without being linked to
        # them: created before the volunteer record, or held by a spouse (email
        # is unique on app_user, volunteer.email is not). bulk_provision adopts
        # an unlinked one, but that is an admin acting parish-wide — linking an
        # existing login to a volunteer hands whoever holds it this volunteer's
        # identity, which is more than "invite my team member" should reach.
        clash = await get_by_email(session, addr)
        if clash is not None:
            return invalid(
                f"{addr} already signs in to another account — a shared address "
                "can only hold one. Ask a parish admin to sort out the link."
            )
        # actor=None: this function's own check (can_invite_volunteer, in the
        # caller) is deliberately wider than account management, and the account
        # it mints is always non-admin and linked to that one volunteer
        made = await create(session, addr, volunteer_id=volunteer_id, invite=invite)
        if isinstance(made, Err):
            return made
        user, token = made.value
        # create() arms an invite whenever no password is given, which is our
        # case; the fallback keeps the return type honest rather than asserting.
        token = token or _issue_invite(user, invite)
        return Ok(Outcome((user, token), (_invite_issued(user, token, invite),)))

    if not account.is_active:
        return invalid(
            f"{volunteer.full_name}'s account is switched off — "
            "a parish admin can switch it back on"
        )
    # Deliberately not reissue_invite(): that clears password_hash so an admin
    # can force a reset, and a leader must never be able to invalidate a working
    # password. Re-arming is only safe on an account nobody has ever used.
    if account.password_hash is not None or account.last_login_at is not None:
        return invalid(
            f"{volunteer.full_name} already has a working account — "
            "they can sign in, or reset from the login page"
        )
    token = _issue_invite(account, invite)
    await session.flush()
    return Ok(Outcome((account, token), (_invite_issued(account, token, invite),)))


async def redeem_invite(
    session: AsyncSession,
    token: str,
    password: str | None,
    *,
    agreed_to_confidentiality: bool,
    now: datetime,
    site_terms: frozenset[str] = frozenset(),
) -> Result[Outcome[AppUser], DomainError]:
    """Complete account setup. The password is optional: without one the
    account stays passwordless and signs in with emailed one-time codes.
    The InviteRedeemed event is the welcome the policy mails and logs.

    Redemption requires accepting the confidentiality notice — agreeing not
    to disclose volunteers' personal information without their consent. The
    UI gates on the checkbox; enforcing it here too means no caller can mint
    an account that skipped the notice, and confidentiality_agreed_at only
    ever records real acceptance.

    An expired link is refused exactly like an unknown one — the same
    NotFound, no hint about which it was."""
    if not agreed_to_confidentiality:
        return invalid("the confidentiality agreement must be accepted")
    if not token:
        return not_found("invite link")
    user = (
        await session.execute(
            sa.select(AppUser).where(AppUser.invite_token == _token_digest(token))
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or not invite_live(user, now):
        return not_found("invite link")
    if password:
        if weak := check_password(password, email=user.email, site_terms=site_terms):
            return weak
        user.password_hash = await async_hash_password(password)
    user.confidentiality_agreed_at = now
    _clear_invite(user)
    await session.flush()
    return Ok(
        Outcome(
            user, (InviteRedeemed(user.id, user.email, has_password=bool(password)),)
        )
    )


async def start_otp_login(
    session: AsyncSession, email: str, *, now: datetime, code: str
) -> Result[Outcome[AppUser], NotFound]:
    """Begin an email one-time-code login.

    NotFound for an unknown or inactive account (callers stay neutral to
    avoid enumeration); an Outcome with no event when a live code was sent
    moments ago (throttled — don't resend); an Outcome carrying OtpIssued
    with the fresh code the edge minted, for the policy to mail. Only the
    argon2 hash of the code is stored."""
    user = await get_by_email(session, email)
    if user is None or not user.is_active:
        return not_found("account")
    if (
        user.otp_sent_at is not None
        and now - user.otp_sent_at < OTP_RESEND_INTERVAL
        and user.otp_expires_at is not None
        and user.otp_expires_at > now
    ):
        return Ok(Outcome(user))
    user.otp_hash = await async_hash_password(code)
    user.otp_sent_at = now
    user.otp_expires_at = now + OTP_TTL
    user.otp_attempts = 0
    await session.flush()
    return Ok(Outcome(user, (OtpIssued(user.email, code),)))


async def verify_otp(
    session: AsyncSession, email: str, code: str, *, now: datetime
) -> Result[AppUser, BadCredentials]:
    user = await get_by_email(session, email)
    if user is None or not user.is_active or user.otp_hash is None:
        return Err(BadCredentials("no code outstanding"))
    if user.otp_expires_at is None or user.otp_expires_at < now:
        return Err(BadCredentials("code expired"))
    if user.otp_attempts >= OTP_MAX_ATTEMPTS:
        return Err(BadCredentials("too many attempts"))
    if not await async_verify_password(user.otp_hash, (code or "").strip()):
        user.otp_attempts += 1
        await session.flush()
        return Err(BadCredentials("wrong code"))
    user.otp_hash = None
    user.otp_sent_at = None
    user.otp_expires_at = None
    user.otp_attempts = 0
    _clear_invite(user)  # possession of the email proves the invite
    user.last_login_at = now
    await session.flush()
    return Ok(user)


def _clear_email_change(user: AppUser) -> None:
    user.pending_email = None
    user.email_change_token = None
    user.email_change_expires_at = None


def email_change_live(user: AppUser, now: datetime) -> bool:
    """Whether a requested address change can still be confirmed."""
    return bool(
        user.pending_email
        and user.email_change_token
        and user.email_change_expires_at
        and user.email_change_expires_at > now
    )


async def start_email_change(
    session: AsyncSession,
    user_id: int,
    new_email: str,
    *,
    now: datetime,
    token: str,
) -> Result[Outcome[tuple[AppUser, str]], DomainError]:
    """Stage an address change and arm the link that confirms it. Returns the
    account and the token; the EmailChangeRequested event carries what the
    policy mails *to the new address* (the proof) and to the old one (the
    warning) — nothing on file moves until confirm_email_change redeems it.

    Asking again simply replaces the pending address and its token: the old
    link dies with it, so a typo is corrected by retyping rather than by
    waiting a day.

    A clash with another account's login address is refused here rather than
    left to the unique constraint, because the person can still fix it — but
    confirm_email_change checks again, since the winner is whoever confirms
    first and that is not decided yet."""
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    normalized = normalize_email(new_email)
    if isinstance(normalized, Err):
        return normalized
    addr = normalized.value
    if addr == user.email.strip().lower():
        return invalid("that is already the address on this account")
    other = await get_by_email(session, addr)
    if other is not None and other.id != user.id:
        return invalid("another account already signs in with that address")
    user.pending_email = addr
    user.email_change_token = _token_digest(token)  # digest only, like invite_token
    user.email_change_expires_at = now + EMAIL_CHANGE_TTL
    await session.flush()
    return Ok(
        Outcome(
            (user, token),
            (
                EmailChangeRequested(
                    user_id=user.id,
                    old_email=user.email,
                    new_email=addr,
                    token=token,
                    ttl_hours=int(EMAIL_CHANGE_TTL.total_seconds() // 3600),
                ),
            ),
        )
    )


async def cancel_email_change(
    session: AsyncSession, user_id: int
) -> Result[Outcome[AppUser], DomainError]:
    """Drop a pending change, killing its link."""
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    _clear_email_change(user)
    await session.flush()
    return Ok(Outcome(user, (EmailChangeCancelled(user.id, user.email),)))


async def pending_email_change(
    session: AsyncSession, token: str, *, now: datetime
) -> AppUser | None:
    """The account a live confirmation link belongs to, without redeeming it.

    The link has to be *shown* before it is *spent*: mail scanners and link
    prefetchers follow URLs on their own, and a single-use token that acts on
    a GET is a token consumed by the recipient's antivirus before they ever
    click. Same reason /invite/ asks for a button press."""
    if not token:
        return None
    user = (
        await session.execute(
            sa.select(AppUser).where(AppUser.email_change_token == _token_digest(token))
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or not email_change_live(user, now):
        return None
    return user


async def confirm_email_change(
    session: AsyncSession, token: str, *, now: datetime
) -> Result[Outcome[tuple[AppUser, str]], DomainError]:
    """Redeem the link: the address moves, and moves everywhere. Returns the
    account and the address it moved *away* from; the EmailChanged event is
    the §4.1.2 receipt the policy sends that old mailbox (this instance is
    mutated in place, so nothing downstream could recover the old address).

    The account's login address and the volunteer record behind it are set
    together — one person, one address. That is what carries the change into
    every team they serve on: rosters, event notices, substitution calls, the
    exported spreadsheet and the Drive share ACLs all read volunteer.email
    live, so an address confirmed here is the address every one of their
    memberships uses from the next send onwards.

    An expired link is refused exactly like an unknown one — the same
    NotFound, no hint about which it was."""
    user = await pending_email_change(session, token, now=now)
    if user is None:
        return not_found("confirmation link")
    previous = user.email  # the mailbox being moved away from
    addr = user.pending_email
    taken = await get_by_email(session, addr)
    if taken is not None and taken.id != user.id:
        # somebody else confirmed the same address inside the window
        _clear_email_change(user)
        await session.flush()
        return invalid("another account has taken that address")
    user.email = addr
    # A code in flight was mailed to the *old* mailbox to prove control of it.
    # The identifier it proved control of no longer exists, so it must not be
    # spendable against the new one.
    user.otp_hash = None
    user.otp_sent_at = None
    user.otp_expires_at = None
    user.otp_attempts = 0
    if user.volunteer_id is not None:
        volunteer = await session.get(Volunteer, user.volunteer_id)
        if volunteer is not None:
            volunteer.email = addr
    _clear_email_change(user)
    await session.flush()
    return Ok(
        Outcome((user, previous), (EmailChanged(user.id, was=previous, now=addr),))
    )


async def issue_api_token(
    session: AsyncSession, user_id: int, *, token: str
) -> Result[str, DomainError]:
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    user.api_token = _token_digest(token)  # only the digest is stored
    await session.flush()
    return Ok(token)


async def ensure_calendar_token(
    session: AsyncSession, user_id: int, *, token: str
) -> Result[str, DomainError]:
    """The account's personal feed address, minted on first request and then
    stable — a subscription is only worth having if it keeps working. `token`
    is the fresh one to use if none is set yet."""
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    if user.calendar_token is None:
        user.calendar_token = token
        await session.flush()
    return Ok(user.calendar_token)


async def reset_calendar_token(
    session: AsyncSession, user_id: int, *, token: str
) -> Result[str, DomainError]:
    """A new address; every client subscribed to the old one goes dark."""
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    user.calendar_token = token
    await session.flush()
    return Ok(user.calendar_token)


async def by_calendar_token(session: AsyncSession, token: str) -> AppUser | None:
    """The active account behind a feed address, or None."""
    if not token:
        return None
    return await session.scalar(
        sa.select(AppUser).where(
            AppUser.calendar_token == token, AppUser.is_active.is_(True)
        )
    )


async def set_flags(
    session: AsyncSession,
    user_id: int,
    *,
    actor: Actor | None = None,
    is_admin: bool | None = None,
    is_active: bool | None = None,
) -> Result[AppUser, DomainError]:
    """Promote/demote an admin, or switch an account off. Admin-only, and the
    only way `is_admin` is ever written — no self-service path touches it."""
    if denied := _require_admin(actor):
        return denied
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    if is_admin is not None:
        user.is_admin = is_admin
    if is_active is not None:
        user.is_active = is_active
    await session.flush()
    return Ok(user)


async def set_volunteer(
    session: AsyncSession,
    user_id: int,
    volunteer_id: int | None,
    *,
    actor: Actor | None = None,
) -> Result[AppUser, DomainError]:
    """Point an account at a volunteer record, or at none.

    The way to correct a wrong auto-link, and the only way to link an account
    created before its volunteer existed. One account per volunteer, so
    claiming someone else's is refused rather than left to the constraint."""
    if denied := _require_admin(actor):
        return denied
    user = await get(session, user_id)
    if user is None:
        return not_found("user", user_id)
    if volunteer_id is not None:
        if await session.get(Volunteer, volunteer_id) is None:
            return not_found("volunteer", volunteer_id)
        other = (
            await session.execute(
                sa.select(AppUser.email).where(
                    AppUser.volunteer_id == volunteer_id, AppUser.id != user_id
                )
            )
        ).scalar_one_or_none()
        if other is not None:
            return invalid(f"that volunteer is already linked to {other}")
    user.volunteer_id = volunteer_id
    await session.flush()
    return Ok(user)


@dataclass
class ProvisionReport:
    # (volunteer, account, plaintext invite token) — the token is only readable
    # here, at the moment it is minted (services.users._issue_invite stores a
    # digest), so a caller that means to mail it must do so from this report.
    created: list[tuple[Volunteer, AppUser, str]] = field(default_factory=list)
    linked: list[tuple[Volunteer, AppUser]] = field(
        default_factory=list
    )  # adopted an account that already existed at that address
    skipped: list[tuple[Volunteer, str]] = field(
        default_factory=list
    )  # (volunteer, reason)


async def bulk_provision(
    session: AsyncSession,
    actor: Actor | None = None,
    *,
    mint: Callable[[], Invite],
) -> Result[Outcome[ProvisionReport], DomainError]:
    """Give every active volunteer with an email an account they can sign in to.
    One InviteIssued event per account created: the policy mails each link.

    A volunteer with no account gets a fresh invite-token one -- `mint` is
    the edge's supply of them, one per account, since the number needed is
    not known until the roster has been read. A volunteer whose address
    already belongs to an *unlinked* account adopts that account instead — it
    was created before the volunteer record existed, or without picking them
    from the dropdown, and would otherwise sign in to an empty app forever.
    Volunteers sharing an email (families) get one account for the first and
    a skip report for the rest."""
    if denied := _require_admin(actor):
        return denied
    report = ProvisionReport()
    issued: list[InviteIssued] = []
    volunteers = list(
        (
            await session.execute(
                sa.select(Volunteer)
                .where(Volunteer.is_active, Volunteer.email.is_not(None))
                .order_by(Volunteer.id)
            )
        ).scalars()
    )
    linked = {
        v_id
        for (v_id,) in await session.execute(
            sa.select(AppUser.volunteer_id).where(AppUser.volunteer_id.is_not(None))
        )
    }
    by_email = {
        u.email: u for u in (await session.execute(sa.select(AppUser))).scalars()
    }
    for v in volunteers:
        email = (v.email or "").strip().lower()
        existing = by_email.get(email)
        if v.id in linked:
            report.skipped.append((v, "already has an account"))
        elif existing is None:
            invite = mint()
            made = await create(
                session, email, actor=actor, volunteer_id=v.id, invite=invite
            )
            if isinstance(made, Err):
                return made
            user, token = made.value
            by_email[email] = user
            linked.add(v.id)
            token = token or _issue_invite(user, invite)
            report.created.append((v, user, token))
            issued.append(_invite_issued(user, token, invite))
        elif existing.volunteer_id is None:
            existing.volunteer_id = v.id
            linked.add(v.id)
            report.linked.append((v, existing))
        else:
            report.skipped.append((v, f"email {email} already used by another account"))
    await session.flush()
    return Ok(Outcome(report, tuple(issued)))
