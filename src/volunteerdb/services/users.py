from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import hash_password, new_otp_code, new_token, verify_password
from ..models import AppUser, Volunteer

OTP_TTL = timedelta(minutes=10)
OTP_RESEND_INTERVAL = timedelta(seconds=60)
OTP_MAX_ATTEMPTS = 5


async def get(session: AsyncSession, user_id: int) -> AppUser | None:
    return await session.get(AppUser, user_id)


async def get_by_email(session: AsyncSession, email: str) -> AppUser | None:
    return (
        await session.execute(sa.select(AppUser).where(AppUser.email == email.strip().lower()))
    ).scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[AppUser]:
    return list((await session.execute(sa.select(AppUser).order_by(AppUser.email))).scalars())


async def authenticate(session: AsyncSession, email: str, password: str) -> AppUser | None:
    user = await get_by_email(session, email)
    if user is None or not user.is_active or user.password_hash is None:
        return None
    if not verify_password(user.password_hash, password):
        return None
    user.last_login_at = sa.func.now()
    await session.flush()
    return user


async def authenticate_token(session: AsyncSession, token: str) -> AppUser | None:
    if not token:
        return None
    user = (
        await session.execute(sa.select(AppUser).where(AppUser.api_token == token))
    ).scalar_one_or_none()
    return user if user is not None and user.is_active else None


async def create(
    session: AsyncSession,
    email: str,
    *,
    volunteer_id: int | None = None,
    is_admin: bool = False,
    password: str | None = None,
) -> AppUser:
    """Create an account. Without a password it gets an invite token instead,
    to be redeemed via the invite link (emailed to the user, or handed out)."""
    user = AppUser(
        email=email.strip().lower(),
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
    user.api_token = new_token()
    await session.flush()
    return user.api_token


async def set_flags(
    session: AsyncSession, user_id: int, *, is_admin: bool | None = None, is_active: bool | None = None
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


@dataclass
class ProvisionReport:
    created: list[tuple[Volunteer, AppUser]] = field(default_factory=list)
    skipped: list[tuple[Volunteer, str]] = field(default_factory=list)  # (volunteer, reason)


async def bulk_provision(session: AsyncSession) -> ProvisionReport:
    """Create invite-token accounts for every active volunteer with an email
    and no account yet. Volunteers sharing an email (families) get one account
    for the first and a skip report for the rest."""
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
    used_emails = {email for (email,) in await session.execute(sa.select(AppUser.email))}
    for v in volunteers:
        email = (v.email or "").strip().lower()
        if v.id in linked:
            report.skipped.append((v, "already has an account"))
        elif email in used_emails:
            report.skipped.append((v, f"email {email} already used by another account"))
        else:
            user = await create(session, email, volunteer_id=v.id)
            used_emails.add(email)
            report.created.append((v, user))
    return report
