import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    burn_password_check,
    hash_password,
    new_otp_code,
    new_token,
    verify_password,
)
from ..models import AppUser, Volunteer
from . import volunteers as volunteer_service

OTP_TTL = timedelta(minutes=10)
OTP_RESEND_INTERVAL = timedelta(seconds=60)
OTP_MAX_ATTEMPTS = 5


async def get(session: AsyncSession, user_id: int) -> AppUser | None:
    return await session.get(AppUser, user_id)


async def get_by_email(session: AsyncSession, email: str) -> AppUser | None:
    return (
        await session.execute(
            sa.select(AppUser).where(AppUser.email == email.strip().lower())
        )
    ).scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[AppUser]:
    return list(
        (await session.execute(sa.select(AppUser).order_by(AppUser.email))).scalars()
    )


async def authenticate(
    session: AsyncSession, email: str, password: str
) -> AppUser | None:
    user = await get_by_email(session, email)
    if user is None or not user.is_active or user.password_hash is None:
        burn_password_check(password)  # uniform timing: no account-existence oracle
        return None
    if not verify_password(user.password_hash, password):
        return None
    user.last_login_at = sa.func.now()
    await session.flush()
    return user


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
    volunteer_id: int | None = None,
    is_admin: bool = False,
    password: str | None = None,
    link_by_email: bool = True,
) -> AppUser:
    """Create an account. Without a password it gets an invite token instead,
    to be redeemed via the invite link (emailed to the user, or handed out).

    With no volunteer_id given, the account adopts the volunteer holding the
    same email address when exactly one does — an account exists to be some
    volunteer's login, and an unlinked one sees an empty app. Pass
    link_by_email=False for accounts that are not a person (the sync bot).
    """
    addr = email.strip().lower()
    if volunteer_id is None and link_by_email:
        volunteer_id = await _volunteer_for_email(session, addr)
    user = AppUser(
        email=addr,
        volunteer_id=volunteer_id,
        is_admin=is_admin,
        password_hash=hash_password(password) if password else None,
        invite_token=None if password else new_token(),
    )
    session.add(user)
    await session.flush()
    return user


async def set_password(session: AsyncSession, user_id: int, password: str) -> None:
    user = await get(session, user_id)
    if user is None:
        raise LookupError(f"user {user_id} not found")
    user.password_hash = hash_password(password)
    user.invite_token = None
    await session.flush()


async def reissue_invite(session: AsyncSession, user_id: int) -> str:
    """New invite link for a user who lost their password. Invalidates the old password."""
    user = await get(session, user_id)
    if user is None:
        raise LookupError(f"user {user_id} not found")
    user.invite_token = new_token()
    user.password_hash = None
    await session.flush()
    return user.invite_token


async def redeem_invite(
    session: AsyncSession, token: str, password: str | None
) -> AppUser | None:
    """Complete account setup. The password is optional: without one the
    account stays passwordless and signs in with emailed one-time codes."""
    if not token:
        return None
    user = (
        await session.execute(sa.select(AppUser).where(AppUser.invite_token == token))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    if password:
        user.password_hash = hash_password(password)
    user.invite_token = None
    await session.flush()
    return user


async def start_otp_login(
    session: AsyncSession, email: str
) -> tuple[AppUser, str | None] | None:
    """Begin an email one-time-code login.

    Returns None for an unknown or inactive account (callers stay neutral to
    avoid enumeration), (user, None) when a live code was sent moments ago
    (throttled — don't resend), or (user, code) with a fresh code to email.
    Only the argon2 hash of the code is stored."""
    user = await get_by_email(session, email)
    if user is None or not user.is_active:
        return None
    now = datetime.now(UTC)
    if (
        user.otp_sent_at is not None
        and now - user.otp_sent_at < OTP_RESEND_INTERVAL
        and user.otp_expires_at is not None
        and user.otp_expires_at > now
    ):
        return user, None
    code = new_otp_code()
    user.otp_hash = hash_password(code)
    user.otp_sent_at = now
    user.otp_expires_at = now + OTP_TTL
    user.otp_attempts = 0
    await session.flush()
    return user, code


async def verify_otp(session: AsyncSession, email: str, code: str) -> AppUser | None:
    user = await get_by_email(session, email)
    if user is None or not user.is_active or user.otp_hash is None:
        return None
    if user.otp_expires_at is None or user.otp_expires_at < datetime.now(UTC):
        return None
    if user.otp_attempts >= OTP_MAX_ATTEMPTS:
        return None
    if not verify_password(user.otp_hash, (code or "").strip()):
        user.otp_attempts += 1
        await session.flush()
        return None
    user.otp_hash = None
    user.otp_sent_at = None
    user.otp_expires_at = None
    user.otp_attempts = 0
    user.invite_token = None  # possession of the email proves the invite
    user.last_login_at = sa.func.now()
    await session.flush()
    return user


async def issue_api_token(session: AsyncSession, user_id: int) -> str:
    user = await get(session, user_id)
    if user is None:
        raise LookupError(f"user {user_id} not found")
    token = new_token()
    user.api_token = _token_digest(token)  # only the digest is stored
    await session.flush()
    return token


async def set_flags(
    session: AsyncSession,
    user_id: int,
    *,
    is_admin: bool | None = None,
    is_active: bool | None = None,
) -> AppUser:
    user = await get(session, user_id)
    if user is None:
        raise LookupError(f"user {user_id} not found")
    if is_admin is not None:
        user.is_admin = is_admin
    if is_active is not None:
        user.is_active = is_active
    await session.flush()
    return user


async def set_volunteer(
    session: AsyncSession, user_id: int, volunteer_id: int | None
) -> AppUser:
    """Point an account at a volunteer record, or at none.

    The way to correct a wrong auto-link, and the only way to link an account
    created before its volunteer existed. One account per volunteer, so
    claiming someone else's is refused rather than left to the constraint."""
    user = await get(session, user_id)
    if user is None:
        raise LookupError(f"user {user_id} not found")
    if volunteer_id is not None:
        if await session.get(Volunteer, volunteer_id) is None:
            raise LookupError(f"volunteer {volunteer_id} not found")
        other = (
            await session.execute(
                sa.select(AppUser.email).where(
                    AppUser.volunteer_id == volunteer_id, AppUser.id != user_id
                )
            )
        ).scalar_one_or_none()
        if other is not None:
            raise ValueError(f"that volunteer is already linked to {other}")
    user.volunteer_id = volunteer_id
    await session.flush()
    return user


@dataclass
class ProvisionReport:
    created: list[tuple[Volunteer, AppUser]] = field(default_factory=list)
    linked: list[tuple[Volunteer, AppUser]] = field(
        default_factory=list
    )  # adopted an account that already existed at that address
    skipped: list[tuple[Volunteer, str]] = field(
        default_factory=list
    )  # (volunteer, reason)


async def bulk_provision(session: AsyncSession) -> ProvisionReport:
    """Give every active volunteer with an email an account they can sign in to.

    A volunteer with no account gets a fresh invite-token one. A volunteer
    whose address already belongs to an *unlinked* account adopts that account
    instead — it was created before the volunteer record existed, or without
    picking them from the dropdown, and would otherwise sign in to an empty
    app forever. Volunteers sharing an email (families) get one account for
    the first and a skip report for the rest."""
    report = ProvisionReport()
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
            user = await create(session, email, volunteer_id=v.id)
            by_email[email] = user
            linked.add(v.id)
            report.created.append((v, user))
        elif existing.volunteer_id is None:
            existing.volunteer_id = v.id
            linked.add(v.id)
            report.linked.append((v, existing))
        else:
            report.skipped.append((v, f"email {email} already used by another account"))
    await session.flush()
    return report
