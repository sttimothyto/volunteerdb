"""Elections: the nomination + STAR-voting pipeline.

Vacancies themselves need no endpoint: GET /api/reports/coverage already
returns missing_leader/missing_second with the same admin-or-leader scoping.
Ballots are secret: these routes return turnout flags and post-conclusion
aggregates, never a voter's scores.
"""

from fastapi import APIRouter

from ..models import Proposal, TeamRole
from ..permissions import require
from ..services import elections as service
from ..services import volunteers as volunteer_service
from ..star import StarResult
from .deps import CtxDep
from .schemas import (
    AppointIn,
    AssignmentOut,
    BallotIn,
    CandidateIn,
    CandidateOut,
    CandidateTallyOut,
    NewRoundIn,
    ProposalCreateIn,
    ProposalDetailOut,
    ProposalOut,
    ProposalPatch,
    TallyOut,
    TeamOut,
    VoterIn,
    VoterOut,
    role_label,
)

router = APIRouter(prefix="/elections", tags=["elections"])


def proposal_out(proposal: Proposal) -> ProposalOut:
    out = ProposalOut.model_validate(proposal)
    out.role_label = role_label(TeamRole(proposal.role))
    phase = service.phase_of(proposal, service.local_today())
    out.phase = phase.value if phase else None
    return out


def _tally_out(result: StarResult, names: dict[int, str]) -> TallyOut:
    ranked = sorted(result.totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return TallyOut(
        ballot_count=result.ballot_count,
        totals=[
            CandidateTallyOut(
                candidate_id=cid, volunteer_name=names.get(cid, ""), total=total
            )
            for cid, total in ranked
        ],
        finalist_ids=list(result.finalist_ids) if result.finalist_ids else None,
        runoff=dict(result.runoff) if result.runoff else None,
        no_preference=result.no_preference,
        winner_candidate_id=result.winner_id,
        tie=result.tie,
        tied_candidate_ids=list(result.tied_ids),
    )


def _detail_out(view: service.ProposalDetail) -> ProposalDetailOut:
    candidates = []
    for c in view.candidates:
        out = CandidateOut.model_validate(c.candidate)
        out.volunteer_name = c.volunteer.full_name
        out.assignments = [
            AssignmentOut(
                membership_id=m.id,
                team=TeamOut.model_validate(t),
                role=m.role,
                role_label=role_label(m.role),
            )
            for m, t in c.assignments
        ]
        candidates.append(out)
    voters = []
    for v in view.voters:
        out = VoterOut.model_validate(v.voter)
        out.volunteer_name = v.volunteer.full_name
        out.has_account = v.has_account
        out.has_voted = v.has_voted
        voters.append(out)
    names = {c.candidate.id: c.volunteer.full_name for c in view.candidates}
    return ProposalDetailOut(
        proposal=proposal_out(view.proposal),
        path=view.path,
        candidates=candidates,
        voters=voters,
        tally=_tally_out(view.tally, names) if view.tally else None,
    )


@router.get("/proposals")
async def list_proposals(
    ctx: CtxDep, team_id: int | None = None, status: str | None = None
) -> list[ProposalOut]:
    """Proposals the caller may see: admins all, leaders/seconds their
    subtree, voters the rolls they sit on."""
    require(ctx.actor.can_access_elections, "use the elections page")
    views = await service.list_proposals(
        ctx.session, ctx.actor, team_id=team_id, status=status
    )
    return [proposal_out(v.proposal) for v in views]


@router.post("/proposals", status_code=201)
async def create_proposal(ctx: CtxDep, data: ProposalCreateIn) -> ProposalOut:
    proposal = await service.create_proposal(
        ctx.session,
        ctx.actor,
        team_id=data.team_id,
        role=data.role,
        nomination_deadline=data.nomination_deadline,
        voting_deadline=data.voting_deadline,
        created_by=ctx.actor.user.id,
        candidates=[
            service.CandidateInput(c.volunteer_id, c.note) for c in data.candidates
        ],
        notes=data.notes,
    )
    return proposal_out(proposal)


@router.get("/proposals/{proposal_id}")
async def get_proposal(ctx: CtxDep, proposal_id: int) -> ProposalDetailOut:
    return _detail_out(await service.detail(ctx.session, ctx.actor, proposal_id))


@router.patch("/proposals/{proposal_id}")
async def update_proposal(
    ctx: CtxDep, proposal_id: int, data: ProposalPatch
) -> ProposalOut:
    extra = {} if data.notes is None else {"notes": data.notes}
    updated = await service.update_proposal(
        ctx.session,
        ctx.actor,
        proposal_id,
        nomination_deadline=data.nomination_deadline,
        voting_deadline=data.voting_deadline,
        **extra,
    )
    return proposal_out(updated)


@router.post("/proposals/{proposal_id}/candidates", status_code=201)
async def add_candidate(
    ctx: CtxDep, proposal_id: int, data: CandidateIn
) -> CandidateOut:
    """Nominate: managers and the proposal's voting members, until the
    nomination deadline."""
    candidate = await service.add_candidate(
        ctx.session,
        ctx.actor,
        proposal_id,
        volunteer_id=data.volunteer_id,
        nominated_by=ctx.actor.user.id,
        note=data.note,
    )
    out = CandidateOut.model_validate(candidate)
    volunteer = await volunteer_service.get(ctx.session, data.volunteer_id)
    out.volunteer_name = volunteer.full_name if volunteer else ""
    return out


@router.delete("/proposals/{proposal_id}/candidates/{candidate_id}", status_code=204)
async def remove_candidate(ctx: CtxDep, proposal_id: int, candidate_id: int) -> None:
    await service.remove_candidate(ctx.session, ctx.actor, proposal_id, candidate_id)


@router.post("/proposals/{proposal_id}/voters", status_code=201)
async def add_voter(ctx: CtxDep, proposal_id: int, data: VoterIn) -> VoterOut:
    voter = await service.add_voter(
        ctx.session,
        ctx.actor,
        proposal_id,
        volunteer_id=data.volunteer_id,
        added_by=ctx.actor.user.id,
    )
    out = VoterOut.model_validate(voter)
    volunteer = await volunteer_service.get(ctx.session, data.volunteer_id)
    out.volunteer_name = volunteer.full_name if volunteer else ""
    return out


@router.delete("/proposals/{proposal_id}/voters/{voter_id}", status_code=204)
async def remove_voter(ctx: CtxDep, proposal_id: int, voter_id: int) -> None:
    await service.remove_voter(ctx.session, ctx.actor, proposal_id, voter_id)


@router.put("/proposals/{proposal_id}/ballot", status_code=204)
async def cast_ballot(ctx: CtxDep, proposal_id: int, data: BallotIn) -> None:
    """Cast or revise the caller's whole ballot (PUT: idempotent overwrite)."""
    require(ctx.actor.volunteer_id is not None, "vote on this proposal")
    await service.cast_ballot(
        ctx.session,
        ctx.actor,
        proposal_id,
        voter_volunteer_id=ctx.actor.volunteer_id,
        scores=data.scores,
    )


@router.get("/proposals/{proposal_id}/tally")
async def get_tally(ctx: CtxDep, proposal_id: int) -> TallyOut:
    """The STAR result — 422 while voting is still possible."""
    view = await service.detail(ctx.session, ctx.actor, proposal_id)
    if view.tally is None:
        raise ValueError("voting has not concluded")
    names = {c.candidate.id: c.volunteer.full_name for c in view.candidates}
    return _tally_out(view.tally, names)


@router.post("/proposals/{proposal_id}/appoint")
async def appoint(ctx: CtxDep, proposal_id: int, data: AppointIn) -> ProposalOut:
    """Appoint: flips the status and creates/upgrades the membership together.
    The tally is advisory — any candidate may be appointed."""
    return proposal_out(
        await service.appoint(
            ctx.session,
            ctx.actor,
            proposal_id,
            data.candidate_id,
            decided_by=ctx.actor.user.id,
        )
    )


@router.post("/proposals/{proposal_id}/cancel")
async def cancel(ctx: CtxDep, proposal_id: int) -> ProposalOut:
    return proposal_out(
        await service.cancel(
            ctx.session, ctx.actor, proposal_id, decided_by=ctx.actor.user.id
        )
    )


@router.post("/proposals/{proposal_id}/new-round", status_code=201)
async def new_round(ctx: CtxDep, proposal_id: int, data: NewRoundIn) -> ProposalOut:
    """Close a concluded round and open a fresh one for the same seat with
    the same candidates and roll — the Ignatian repeat."""
    return proposal_out(
        await service.new_round(
            ctx.session,
            ctx.actor,
            proposal_id,
            created_by=ctx.actor.user.id,
            nomination_deadline=data.nomination_deadline,
            voting_deadline=data.voting_deadline,
        )
    )
