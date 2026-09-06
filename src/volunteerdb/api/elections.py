"""Elections: the nomination + STAR-voting pipeline.

Vacancies themselves need no endpoint: GET /api/reports/coverage already
returns missing_leader/missing_second with the same admin-or-leader scoping.
Ballots are secret: these routes return turnout flags and post-conclusion
aggregates, never a voter's scores.
"""

from fastapi import APIRouter, HTTPException

from ..services import elections as service
from ..services import volunteers as volunteer_service
from .deps import CtxDep, gate, raise_http
from .schemas import (
    AppointIn,
    BallotIn,
    BallotOut,
    CandidateIn,
    CandidateOut,
    CoverageOut,
    NewRoundIn,
    ProposalCreateIn,
    ProposalDetailOut,
    ProposalOut,
    ProposalPatch,
    TallyOut,
    VoterIn,
    VoterOut,
)

router = APIRouter(prefix="/elections", tags=["elections"])


@router.get("/proposals")
async def list_proposals(
    ctx: CtxDep, team_id: int | None = None, status: str | None = None
) -> list[ProposalOut]:
    """Proposals the caller may see: admins all, leaders/seconds their
    subtree, voters the rolls they sit on."""
    gate(ctx.actor.can_access_elections, "use the elections page")
    views = await service.list_proposals(
        ctx.session, ctx.actor, team_id=team_id, status=status, today=ctx.env.today()
    )
    return [ProposalOut.of(v.proposal, today=ctx.env.today()) for v in views]


@router.post("/proposals", status_code=201)
async def create_proposal(ctx: CtxDep, data: ProposalCreateIn) -> ProposalOut:
    proposal = raise_http(
        await service.create_proposal(
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
            today=ctx.env.today(),
        )
    )
    return ProposalOut.of(proposal, today=ctx.env.today())


@router.get("/proposals/{proposal_id}")
async def get_proposal(ctx: CtxDep, proposal_id: int) -> ProposalDetailOut:
    view = raise_http(
        await service.detail(ctx.session, ctx.actor, proposal_id, today=ctx.env.today())
    )
    return ProposalDetailOut.of(view, today=ctx.env.today())


@router.patch("/proposals/{proposal_id}")
async def update_proposal(
    ctx: CtxDep, proposal_id: int, data: ProposalPatch
) -> ProposalOut:
    extra = {} if data.notes is None else {"notes": data.notes}
    updated = raise_http(
        await service.update_proposal(
            ctx.session,
            ctx.actor,
            proposal_id,
            nomination_deadline=data.nomination_deadline,
            voting_deadline=data.voting_deadline,
            **extra,
            today=ctx.env.today(),
        )
    )
    return ProposalOut.of(updated, today=ctx.env.today())


@router.post("/proposals/{proposal_id}/candidates", status_code=201)
async def add_candidate(
    ctx: CtxDep, proposal_id: int, data: CandidateIn
) -> CandidateOut:
    """Nominate: managers and the proposal's voting members, until the
    nomination deadline."""
    candidate = raise_http(
        await service.add_candidate(
            ctx.session,
            ctx.actor,
            proposal_id,
            volunteer_id=data.volunteer_id,
            nominated_by=ctx.actor.user.id,
            note=data.note,
            today=ctx.env.today(),
        )
    )
    volunteer = await volunteer_service.get(ctx.session, data.volunteer_id)
    return CandidateOut.of(candidate, volunteer)


@router.delete("/proposals/{proposal_id}/candidates/{candidate_id}", status_code=204)
async def remove_candidate(ctx: CtxDep, proposal_id: int, candidate_id: int) -> None:
    raise_http(
        await service.remove_candidate(
            ctx.session, ctx.actor, proposal_id, candidate_id, today=ctx.env.today()
        )
    )


@router.post("/proposals/{proposal_id}/voters", status_code=201)
async def add_voter(ctx: CtxDep, proposal_id: int, data: VoterIn) -> VoterOut:
    voter = raise_http(
        await service.add_voter(
            ctx.session,
            ctx.actor,
            proposal_id,
            volunteer_id=data.volunteer_id,
            added_by=ctx.actor.user.id,
            today=ctx.env.today(),
        )
    )
    volunteer = await volunteer_service.get(ctx.session, data.volunteer_id)
    return VoterOut.of(voter, volunteer)


@router.delete("/proposals/{proposal_id}/voters/{voter_id}", status_code=204)
async def remove_voter(ctx: CtxDep, proposal_id: int, voter_id: int) -> None:
    raise_http(
        await service.remove_voter(
            ctx.session, ctx.actor, proposal_id, voter_id, today=ctx.env.today()
        )
    )


@router.get("/vacancies")
async def vacancies(ctx: CtxDep) -> list[CoverageOut]:
    """Seats missing a leader or a second — where a proposal would go next.

    The GUI's vacancy list, which had no endpoint: GET /reports/coverage is
    close but returns every team with its counts, not the holes. Scoped like
    the proposal list: admins see the parish, leaders/seconds their subtree.
    """
    gate(ctx.actor.can_access_elections, "use the elections page")
    rows = await service.vacancies(ctx.session, ctx.actor)
    return [CoverageOut.of(r) for r in rows]


@router.get("/proposals/{proposal_id}/ballot")
async def get_own_ballot(ctx: CtxDep, proposal_id: int) -> BallotOut:
    """The caller's own scores, so a ballot can be revised rather than
    overwritten blind. A ballot is secret from everyone else — including the
    team's managers — so this only ever answers for the caller themselves.
    Empty until they vote."""
    gate(ctx.actor.volunteer_id is not None, "vote on this proposal")
    scores = raise_http(
        await service.my_scores(
            ctx.session, ctx.actor, proposal_id, ctx.actor.volunteer_id
        )
    )
    return BallotOut(scores=scores)


@router.put("/proposals/{proposal_id}/ballot", status_code=204)
async def cast_ballot(ctx: CtxDep, proposal_id: int, data: BallotIn) -> None:
    """Cast or revise the caller's whole ballot (PUT: idempotent overwrite)."""
    gate(ctx.actor.volunteer_id is not None, "vote on this proposal")
    raise_http(
        await service.cast_ballot(
            ctx.session,
            ctx.actor,
            proposal_id,
            voter_volunteer_id=ctx.actor.volunteer_id,
            scores=data.scores,
            today=ctx.env.today(),
            now=ctx.now,
        )
    )


@router.get("/proposals/{proposal_id}/tally")
async def get_tally(ctx: CtxDep, proposal_id: int) -> TallyOut:
    """The STAR result — 422 while voting is still possible."""
    view = raise_http(
        await service.detail(ctx.session, ctx.actor, proposal_id, today=ctx.env.today())
    )
    if view.tally is None:
        raise HTTPException(422, "voting has not concluded")
    names = {c.candidate.id: c.volunteer.full_name for c in view.candidates}
    return TallyOut.of(view.tally, names)


@router.post("/proposals/{proposal_id}/appoint")
async def appoint(ctx: CtxDep, proposal_id: int, data: AppointIn) -> ProposalOut:
    """Appoint: flips the status and creates/upgrades the membership together.
    The tally is advisory — any candidate may be appointed."""
    return ProposalOut.of(
        raise_http(
            await service.appoint(
                ctx.session,
                ctx.actor,
                proposal_id,
                data.candidate_id,
                decided_by=ctx.actor.user.id,
                today=ctx.env.today(),
                now=ctx.now,
            )
        ),
        today=ctx.env.today(),
    )


@router.post("/proposals/{proposal_id}/cancel")
async def cancel(ctx: CtxDep, proposal_id: int) -> ProposalOut:
    return ProposalOut.of(
        raise_http(
            await service.cancel(
                ctx.session,
                ctx.actor,
                proposal_id,
                decided_by=ctx.actor.user.id,
                now=ctx.now,
            )
        ),
        today=ctx.env.today(),
    )


@router.post("/proposals/{proposal_id}/new-round", status_code=201)
async def new_round(ctx: CtxDep, proposal_id: int, data: NewRoundIn) -> ProposalOut:
    """Close a concluded round and open a fresh one for the same seat with
    the same candidates and roll — the Ignatian repeat."""
    return ProposalOut.of(
        raise_http(
            await service.new_round(
                ctx.session,
                ctx.actor,
                proposal_id,
                created_by=ctx.actor.user.id,
                nomination_deadline=data.nomination_deadline,
                voting_deadline=data.voting_deadline,
                today=ctx.env.today(),
                now=ctx.now,
            )
        ),
        today=ctx.env.today(),
    )
