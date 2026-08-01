"""Collaborative planning: proposals to fill vacant roles.

A vacancy is a team missing a leader or a second-in-command (derived from the
coverage report, never stored). Planners — admins, and leaders/seconds within
their managed subtree — propose volunteers for roles; whoever manages the team
accepts (creating the membership) or declines. Like every service, permission
checks live in the callers via require().
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..models import AppUser, Proposal, ProposalStatus, Team, TeamRole, Volunteer
from ..permissions import Actor
from . import memberships as membership_service
from . import reports as report_service
from . import teams as team_service
from .reports import CoverageRow


async def vacancies(session: AsyncSession, actor: Actor) -> list[CoverageRow]:
    """Coverage rows missing a leader or second, scoped to the actor's teams.
    Admins see all; leaders/seconds see their managed subtree only."""
    rows = await report_service.coverage(session)
    if not actor.is_admin:
        rows = [r for r in rows if r.team.id in actor.managed_team_ids]
    return [r for r in rows if r.missing_leader or r.missing_second]


@dataclass
class ProposalView:
    proposal: Proposal
    volunteer: Volunteer
    team: Team
    path: str
    proposer_email: str | None  # None once the proposing account is deleted
    decider_email: str | None


async def list_proposals(
    session: AsyncSession,
    actor: Actor,
    *,
    team_id: int | None = None,
    status: str | None = None,
) -> list[ProposalView]:
    """Proposals joined for display, newest first, scoped like vacancies().
    Live-only: planning is about now, so there is no as-of variant."""
    if not actor.is_admin and not actor.managed_team_ids:
        return []
    proposer, decider = aliased(AppUser), aliased(AppUser)
    stmt = (
        sa.select(Proposal, Volunteer, Team, proposer.email, decider.email)
        .join(Volunteer, Volunteer.id == Proposal.volunteer_id)
        .join(Team, Team.id == Proposal.team_id)
        .outerjoin(proposer, proposer.id == Proposal.proposed_by)
        .outerjoin(decider, decider.id == Proposal.decided_by)
        .order_by(Proposal.created_at.desc(), Proposal.id.desc())
    )
    if team_id is not None:
        stmt = stmt.where(Proposal.team_id == team_id)
    if status is not None:
        stmt = stmt.where(Proposal.status == status)
    if not actor.is_admin:
        stmt = stmt.where(Proposal.team_id.in_(actor.managed_team_ids))
    paths = team_service.team_paths(await team_service.list_all(session))
    return [
        ProposalView(p, v, t, paths.get(t.id, t.name), p_email, d_email)
        for p, v, t, p_email, d_email in (await session.execute(stmt)).all()
    ]


async def get(session: AsyncSession, proposal_id: int) -> Proposal | None:
    return await session.get(Proposal, proposal_id)


async def propose(
    session: AsyncSession,
    *,
    team_id: int,
    volunteer_id: int,
    role: TeamRole,
    proposed_by: int,
    note: str | None = None,
) -> Proposal:
    """Open a proposal. A duplicate OPEN (team, role, volunteer) violates
    uq_proposal_open and surfaces as IntegrityError (409 / toast)."""
    proposal = Proposal(
        team_id=team_id,
        volunteer_id=volunteer_id,
        role=role,
        proposed_by=proposed_by,
        note=(note or "").strip() or None,
    )
    session.add(proposal)
    await session.flush()
    return proposal


def _decide(proposal: Proposal, status: ProposalStatus, decided_by: int) -> None:
    if proposal.status != ProposalStatus.proposed.value:
        raise ValueError(f"proposal already {proposal.status}")
    proposal.status = status.value
    proposal.decided_by = decided_by
    proposal.decided_at = datetime.now(UTC)


async def _get_or_raise(session: AsyncSession, proposal_id: int) -> Proposal:
    proposal = await get(session, proposal_id)
    if proposal is None:
        raise LookupError(f"proposal {proposal_id} not found")
    return proposal


async def accept(session: AsyncSession, proposal_id: int, *, decided_by: int) -> Proposal:
    """Mark accepted AND create the membership — both flush in the caller's
    session, so they commit (or roll back) together. assign() upserts: a
    volunteer already on the team gets their role updated instead."""
    proposal = await _get_or_raise(session, proposal_id)
    _decide(proposal, ProposalStatus.accepted, decided_by)
    await membership_service.assign(
        session, proposal.volunteer_id, proposal.team_id, TeamRole(proposal.role)
    )
    await session.flush()
    return proposal


async def decline(session: AsyncSession, proposal_id: int, *, decided_by: int) -> Proposal:
    proposal = await _get_or_raise(session, proposal_id)
    _decide(proposal, ProposalStatus.declined, decided_by)
    await session.flush()
    return proposal


async def withdraw(session: AsyncSession, proposal_id: int, *, decided_by: int) -> Proposal:
    proposal = await _get_or_raise(session, proposal_id)
    _decide(proposal, ProposalStatus.withdrawn, decided_by)
    await session.flush()
    return proposal
