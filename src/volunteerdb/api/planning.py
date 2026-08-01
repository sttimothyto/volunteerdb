"""Planning proposals. Vacancies themselves need no endpoint: GET
/api/reports/coverage already returns missing_leader/missing_second with the
same admin-or-leader scoping."""

from fastapi import APIRouter

from ..models import Proposal, TeamRole
from ..permissions import require
from ..services import planning as service
from .deps import CtxDep
from .schemas import ProposalIn, ProposalOut, role_label

router = APIRouter(prefix="/planning", tags=["planning"])


def _out(proposal: Proposal) -> ProposalOut:
    out = ProposalOut.model_validate(proposal)
    out.role_label = role_label(TeamRole(proposal.role))
    return out


@router.get("/proposals")
async def list_proposals(
    ctx: CtxDep, team_id: int | None = None, status: str | None = None
) -> list[ProposalOut]:
    """Proposals the caller may see: admins all, leaders/seconds their subtree."""
    require(
        ctx.actor.is_admin or bool(ctx.actor.managed_team_ids), "use the planning page"
    )
    views = await service.list_proposals(
        ctx.session, ctx.actor, team_id=team_id, status=status
    )
    return [_out(v.proposal) for v in views]


@router.post("/proposals", status_code=201)
async def create_proposal(ctx: CtxDep, data: ProposalIn) -> ProposalOut:
    require(ctx.actor.can_manage_team(data.team_id), "propose for this team")
    proposal = await service.propose(
        ctx.session,
        team_id=data.team_id,
        volunteer_id=data.volunteer_id,
        role=data.role,
        proposed_by=ctx.actor.user.id,
        note=data.note,
    )
    return _out(proposal)


async def _decided(ctx, proposal_id: int) -> Proposal:
    proposal = await service.get(ctx.session, proposal_id)
    if proposal is None:
        raise LookupError(f"proposal {proposal_id} not found")
    require(
        ctx.actor.can_manage_team(proposal.team_id), "decide proposals for this team"
    )
    return proposal


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(ctx: CtxDep, proposal_id: int) -> ProposalOut:
    """Accept: flips the status and creates/upgrades the membership together."""
    await _decided(ctx, proposal_id)
    return _out(
        await service.accept(ctx.session, proposal_id, decided_by=ctx.actor.user.id)
    )


@router.post("/proposals/{proposal_id}/decline")
async def decline_proposal(ctx: CtxDep, proposal_id: int) -> ProposalOut:
    await _decided(ctx, proposal_id)
    return _out(
        await service.decline(ctx.session, proposal_id, decided_by=ctx.actor.user.id)
    )


@router.post("/proposals/{proposal_id}/withdraw")
async def withdraw_proposal(ctx: CtxDep, proposal_id: int) -> ProposalOut:
    """Withdraw: the proposer may retract their own proposal; team managers may too."""
    proposal = await service.get(ctx.session, proposal_id)
    if proposal is None:
        raise LookupError(f"proposal {proposal_id} not found")
    require(
        proposal.proposed_by == ctx.actor.user.id
        or ctx.actor.can_manage_team(proposal.team_id),
        "withdraw this proposal",
    )
    return _out(
        await service.withdraw(ctx.session, proposal_id, decided_by=ctx.actor.user.id)
    )
