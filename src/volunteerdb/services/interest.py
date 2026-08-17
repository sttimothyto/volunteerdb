"""Interest submissions from the public ministry pages.

An outsider (no account, no session) fills the "I'm interested" form on
/ministries/<slug>.html; the submission lands here, leaders see it on the
team page and resolve it once handled. Like every service, permission checks
live in the callers via require() — except submit(), whose caller is the
public route and has no actor at all.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Interest, Membership, TeamRole, Volunteer


def _clip(value: str | None, limit: int) -> str | None:
    trimmed = (value or "").strip()
    return trimmed[:limit] or None


async def submit(
    session: AsyncSession,
    *,
    team_id: int,
    name: str,
    email: str,
    phone: str | None = None,
    note: str | None = None,
) -> Interest | None:
    """Record one submission; None if an open interest for this (team, email)
    already exists — the caller then sends no mail, so repeat POSTs cannot
    re-spam leaders or the applicant. The SELECT is the friendly path; the
    uq_interest_open partial index backstops the race."""
    cleaned_name = _clip(name, 200)
    cleaned_email = _clip(email, 255)
    if not cleaned_name or not cleaned_email or "@" not in cleaned_email:
        raise ValueError("name and a valid email are required")
    cleaned_email = cleaned_email.lower()
    existing = await session.scalar(
        sa.select(Interest.id).where(
            Interest.team_id == team_id,
            Interest.email == cleaned_email,
            Interest.resolved_at.is_(None),
        )
    )
    if existing is not None:
        return None
    interest = Interest(
        team_id=team_id,
        name=cleaned_name,
        email=cleaned_email,
        phone=_clip(phone, 50),
        note=_clip(note, 2000),
    )
    session.add(interest)
    await session.flush()
    return interest


async def unresolved(session: AsyncSession, team_id: int) -> list[Interest]:
    rows = await session.scalars(
        sa.select(Interest)
        .where(Interest.team_id == team_id, Interest.resolved_at.is_(None))
        .order_by(Interest.created_at)
    )
    return list(rows)


async def get(session: AsyncSession, interest_id: int) -> Interest | None:
    return await session.get(Interest, interest_id)


async def resolve(
    session: AsyncSession, interest_id: int, *, resolved_by: int | None
) -> Interest:
    interest = await session.get(Interest, interest_id)
    if interest is None:
        raise LookupError(f"interest {interest_id} not found")
    interest.resolved_at = sa.func.now()
    interest.resolved_by = resolved_by
    await session.flush()
    return interest


async def leader_emails(session: AsyncSession, team_id: int) -> list[str]:
    """Emails of the team's leader(s) and second(s) — who a new interest goes
    to, and who the nightly Drive sync grants edit access to the team's roster
    sheet. Lowercased and blank-free: imports leave '' where a roster had no
    address, and neither a mailer nor a Drive share can do anything with it."""
    rows = await session.scalars(
        sa.select(Volunteer.email)
        .join(Membership, Membership.volunteer_id == Volunteer.id)
        .where(
            Membership.team_id == team_id,
            Membership.role.in_([TeamRole.leader, TeamRole.second]),
            Volunteer.email.is_not(None),
            Volunteer.email != "",
        )
    )
    return sorted({email.strip().lower() for email in rows if email.strip()})
