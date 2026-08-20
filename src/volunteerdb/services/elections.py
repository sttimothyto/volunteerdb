"""Elections: fill a (team, role) seat by nomination and STAR vote.

A vacancy is a team missing a leader or a second-in-command (derived from
the coverage report, never stored). A manager opens a proposal for the
seat; candidates are nominated — each with a "why them" note — until the
nomination deadline, then the voting roll scores every candidate 0-5 until
the voting deadline (STAR, see star.py), then a manager appoints or starts
a new round: the Ignatian rhythm of pray, vote, debate, repeat. The phase
of an open proposal derives from today's date in the parish timezone;
nothing runs on a clock.

Ballots are secret: individual scores leave this module only as their
owner's my_scores() or as post-conclusion aggregates.

Permission checks live HERE, not in the callers: managing a proposal is the
same right as managing the seat's team (_managed), reading one is that or a
seat on its roll (_viewable), nominating is either, and voting — or reading
back your own ballot — is your own seat and nobody else's.
"""

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    AppUser,
    Membership,
    Proposal,
    ProposalBallot,
    ProposalCandidate,
    ProposalStatus,
    ProposalVoter,
    Team,
    TeamRole,
    Volunteer,
)
from ..permissions import Actor, require
from ..star import StarResult, star_tally
from . import memberships as membership_service
from . import reports as report_service
from . import teams as team_service
from .reports import CoverageRow

_UNSET: object = object()


async def vacancies(session: AsyncSession, actor: Actor) -> list[CoverageRow]:
    """Coverage rows missing a leader or second, scoped to the actor's teams.
    Admins see all; leaders/seconds see their managed subtree only."""
    rows = await report_service.coverage(session)
    if not actor.is_admin:
        rows = [r for r in rows if r.team.id in actor.managed_team_ids]
    return [r for r in rows if r.missing_leader or r.missing_second]


# --- phases ------------------------------------------------------------------


class ProposalPhase(enum.StrEnum):
    nominating = "nominating"  # today <= nomination_deadline
    voting = "voting"  # nomination_deadline < today <= voting_deadline
    concluded = "concluded"  # today > voting_deadline, awaiting decision


PHASE_LABELS: dict[ProposalPhase, str] = {
    ProposalPhase.nominating: "Nominating",
    ProposalPhase.voting: "Voting",
    ProposalPhase.concluded: "Awaiting decision",
}


def local_today() -> date:
    """Today in the parish's day (settings().timezone), not the container's."""
    return datetime.now(ZoneInfo(settings().timezone)).date()


def phase_of(proposal: Proposal, today: date) -> ProposalPhase | None:
    """The open proposal's phase; None once decided. Deadlines are inclusive:
    the seat nominates through the end of d1 and votes through the end of d2."""
    if proposal.status != ProposalStatus.open.value:
        return None
    if today <= proposal.nomination_deadline:
        return ProposalPhase.nominating
    if today <= proposal.voting_deadline:
        return ProposalPhase.voting
    return ProposalPhase.concluded


def _require_phase(
    proposal: Proposal, today: date, expected: ProposalPhase, action: str
) -> None:
    phase = phase_of(proposal, today)
    if phase != expected:
        state = phase.value if phase else proposal.status
        raise ValueError(f"cannot {action}: proposal is {state}, not {expected.value}")


def _check_deadlines(
    nomination_deadline: date, voting_deadline: date, today: date
) -> None:
    if nomination_deadline < today:
        raise ValueError("nomination deadline is in the past")
    if voting_deadline <= nomination_deadline:
        raise ValueError("voting deadline must fall after the nomination deadline")


# --- the clergy team ----------------------------------------------------------

# One team votes on every proposal — the parish clergy — and _default_roll
# finds it by this name when it builds a roll, rather than from a stored id.
# Nothing to configure and nothing to dangle: rename or delete the team and
# rolls simply stop including it, which is the intended way to retire the
# standing. If no team is named this, a roll is the target team's own
# leadership and core members alone.
CLERGY_TEAM_NAME = "Clergy"


# --- views -------------------------------------------------------------------


@dataclass
class ProposalSummary:
    proposal: Proposal
    team: Team
    path: str
    phase: ProposalPhase | None
    candidate_count: int
    voter_count: int
    voted_count: int  # voters who have cast a ballot


@dataclass
class ProposalInvolvement:
    """One proposal touching a volunteer, for their profile page."""

    proposal: Proposal
    team: Team
    path: str
    phase: ProposalPhase | None
    as_candidate: bool
    as_voter: bool
    appointed: bool  # this volunteer is the appointed candidate


@dataclass
class CandidateView:
    candidate: ProposalCandidate
    volunteer: Volunteer
    nominator_email: str | None
    assignments: list[tuple[Membership, Team]]  # current commitments


@dataclass
class VoterView:
    voter: ProposalVoter
    volunteer: Volunteer
    has_account: bool  # an inactive/missing account cannot vote
    has_voted: bool


@dataclass
class ProposalDetail:
    proposal: Proposal
    team: Team
    path: str
    phase: ProposalPhase | None
    creator_email: str | None
    decider_email: str | None
    candidates: list[CandidateView]
    voters: list[VoterView]
    tally: StarResult | None  # None until voting has concluded


async def get(session: AsyncSession, proposal_id: int) -> Proposal | None:
    return await session.get(Proposal, proposal_id)


async def _managed(
    session: AsyncSession, actor: Actor | None, proposal_id: int
) -> Proposal:
    """The proposal, for a caller who runs the seat's team.

    Managing a proposal — deadlines, the roll, the candidate list, appointing,
    cancelling, starting a round — is the same right as editing that team's
    roster, because appointing *is* a roster write."""
    proposal = await _get_or_raise(session, proposal_id)
    require(
        actor is None or actor.can_manage_team(proposal.team_id),
        "manage proposals for this team",
    )
    return proposal


async def _viewable(
    session: AsyncSession, actor: Actor | None, proposal_id: int
) -> Proposal:
    """The proposal, for a manager of its team or somebody on its roll. Voters
    keep access after the decision, to see the result they voted on."""
    proposal = await _get_or_raise(session, proposal_id)
    require(
        actor is None or actor.can_view_proposal(proposal_id, proposal.team_id),
        "view this proposal",
    )
    return proposal


async def _get_or_raise(session: AsyncSession, proposal_id: int) -> Proposal:
    proposal = await get(session, proposal_id)
    if proposal is None:
        raise LookupError(f"proposal {proposal_id} not found")
    return proposal


async def _counts(
    session: AsyncSession, proposal_ids: list[int]
) -> dict[int, tuple[int, int, int]]:
    """proposal id -> (candidates, roll size, voters who have voted)."""
    if not proposal_ids:
        return {}
    candidates = dict(
        (
            await session.execute(
                sa.select(ProposalCandidate.proposal_id, sa.func.count())
                .where(ProposalCandidate.proposal_id.in_(proposal_ids))
                .group_by(ProposalCandidate.proposal_id)
            )
        ).all()
    )
    voters = dict(
        (
            await session.execute(
                sa.select(ProposalVoter.proposal_id, sa.func.count())
                .where(ProposalVoter.proposal_id.in_(proposal_ids))
                .group_by(ProposalVoter.proposal_id)
            )
        ).all()
    )
    voted = dict(
        (
            await session.execute(
                sa.select(
                    ProposalBallot.proposal_id,
                    sa.func.count(sa.distinct(ProposalBallot.voter_id)),
                )
                .where(ProposalBallot.proposal_id.in_(proposal_ids))
                .group_by(ProposalBallot.proposal_id)
            )
        ).all()
    )
    return {
        pid: (candidates.get(pid, 0), voters.get(pid, 0), voted.get(pid, 0))
        for pid in proposal_ids
    }


async def list_proposals(
    session: AsyncSession,
    actor: Actor,
    *,
    team_id: int | None = None,
    status: str | None = None,
    today: date | None = None,
) -> list[ProposalSummary]:
    """Proposals joined for display, newest first. Admins see all; everyone
    else sees their managed subtree plus any roll they sit on. Live-only:
    an election is about now, so there is no as-of variant."""
    today = today or local_today()
    if (
        not actor.is_admin
        and not actor.managed_team_ids
        and not actor.voter_proposal_ids
    ):
        return []
    stmt = (
        sa.select(Proposal, Team)
        .join(Team, Team.id == Proposal.team_id)
        .order_by(Proposal.created_at.desc(), Proposal.id.desc())
    )
    if team_id is not None:
        stmt = stmt.where(Proposal.team_id == team_id)
    if status is not None:
        stmt = stmt.where(Proposal.status == status)
    if not actor.is_admin:
        stmt = stmt.where(
            sa.or_(
                Proposal.team_id.in_(actor.managed_team_ids),
                Proposal.id.in_(actor.voter_proposal_ids),
            )
        )
    rows = (await session.execute(stmt)).all()
    paths = (await team_service.tree(session)).paths
    counts = await _counts(session, [p.id for p, _ in rows])
    return [
        ProposalSummary(
            proposal=p,
            team=t,
            path=paths.get(t.id, t.name),
            phase=phase_of(p, today),
            candidate_count=counts[p.id][0],
            voter_count=counts[p.id][1],
            voted_count=counts[p.id][2],
        )
        for p, t in rows
    ]


async def involving(
    session: AsyncSession,
    actor: Actor,
    volunteer_id: int,
    *,
    today: date | None = None,
) -> list[ProposalInvolvement]:
    """Proposals where the volunteer is a candidate or sits on the voting
    roll, newest first, scoped exactly like list_proposals — so a nominee
    without elections access never learns of their own nomination."""
    today = today or local_today()
    if (
        not actor.is_admin
        and not actor.managed_team_ids
        and not actor.voter_proposal_ids
    ):
        return []
    candidate_ids: dict[int, int] = dict(  # proposal id -> their candidate row id
        (
            await session.execute(
                sa.select(ProposalCandidate.proposal_id, ProposalCandidate.id).where(
                    ProposalCandidate.volunteer_id == volunteer_id
                )
            )
        ).all()
    )
    voter_proposal_ids = set(
        (
            await session.execute(
                sa.select(ProposalVoter.proposal_id).where(
                    ProposalVoter.volunteer_id == volunteer_id
                )
            )
        ).scalars()
    )
    involved = set(candidate_ids) | voter_proposal_ids
    if not involved:
        return []
    stmt = (
        sa.select(Proposal, Team)
        .join(Team, Team.id == Proposal.team_id)
        .where(Proposal.id.in_(involved))
        .order_by(Proposal.created_at.desc(), Proposal.id.desc())
    )
    if not actor.is_admin:
        stmt = stmt.where(
            sa.or_(
                Proposal.team_id.in_(actor.managed_team_ids),
                Proposal.id.in_(actor.voter_proposal_ids),
            )
        )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    paths = (await team_service.tree(session)).paths
    return [
        ProposalInvolvement(
            proposal=p,
            team=t,
            path=paths.get(t.id, t.name),
            phase=phase_of(p, today),
            as_candidate=p.id in candidate_ids,
            as_voter=p.id in voter_proposal_ids,
            appointed=p.appointed_candidate_id is not None
            and p.appointed_candidate_id == candidate_ids.get(p.id),
        )
        for p, t in rows
    ]


async def detail(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    today: date | None = None,
) -> ProposalDetail:
    await _viewable(session, actor, proposal_id)
    today = today or local_today()
    proposal = await _get_or_raise(session, proposal_id)
    # tree() first: it loads every Team into the identity map, so the get() below
    # is served from it rather than costing a second round trip on a cold memo
    paths = (await team_service.tree(session)).paths
    team = await session.get(Team, proposal.team_id)

    email_ids = {i for i in (proposal.created_by, proposal.decided_by) if i}
    emails: dict[int, str] = {}
    if email_ids:
        rows = await session.execute(
            sa.select(AppUser.id, AppUser.email).where(AppUser.id.in_(email_ids))
        )
        emails = dict(rows.all())

    candidate_rows = (
        await session.execute(
            sa.select(ProposalCandidate, Volunteer, AppUser.email)
            .join(Volunteer, Volunteer.id == ProposalCandidate.volunteer_id)
            .outerjoin(AppUser, AppUser.id == ProposalCandidate.nominated_by)
            .where(ProposalCandidate.proposal_id == proposal_id)
            .order_by(ProposalCandidate.id)
        )
    ).all()
    candidate_vol_ids = [v.id for _, v, _ in candidate_rows]
    commitments: dict[int, list[tuple[Membership, Team]]] = {
        vid: [] for vid in candidate_vol_ids
    }
    if candidate_vol_ids:
        rows = await session.execute(
            sa.select(Membership, Team)
            .join(Team, Team.id == Membership.team_id)
            .where(Membership.volunteer_id.in_(candidate_vol_ids))
            .order_by(Team.name)
        )
        for m, t in rows:
            commitments[m.volunteer_id].append((m, t))

    voter_rows = (
        await session.execute(
            sa.select(ProposalVoter, Volunteer)
            .join(Volunteer, Volunteer.id == ProposalVoter.volunteer_id)
            .where(ProposalVoter.proposal_id == proposal_id)
            .order_by(Volunteer.last_name, Volunteer.first_name)
        )
    ).all()
    voter_vol_ids = [v.id for _, v in voter_rows]
    account_ids: set[int] = set()
    if voter_vol_ids:
        rows = await session.execute(
            sa.select(AppUser.volunteer_id).where(
                AppUser.volunteer_id.in_(voter_vol_ids), AppUser.is_active
            )
        )
        account_ids = set(rows.scalars())
    voted_ids = set(
        (
            await session.execute(
                sa.select(sa.distinct(ProposalBallot.voter_id)).where(
                    ProposalBallot.proposal_id == proposal_id
                )
            )
        ).scalars()
    )

    return ProposalDetail(
        proposal=proposal,
        team=team,
        path=paths.get(team.id, team.name),
        phase=phase_of(proposal, today),
        creator_email=emails.get(proposal.created_by),
        decider_email=emails.get(proposal.decided_by),
        candidates=[
            CandidateView(c, v, email, commitments[v.id])
            for c, v, email in candidate_rows
        ],
        voters=[
            VoterView(pv, v, v.id in account_ids, pv.id in voted_ids)
            for pv, v in voter_rows
        ],
        tally=(
            await _tally(session, proposal_id)
            if _tally_visible(proposal, today)
            else None
        ),
    )


# --- lifecycle ---------------------------------------------------------------


@dataclass(frozen=True)
class CandidateInput:
    volunteer_id: int
    note: str | None = None


async def _default_roll(session: AsyncSession, team_id: int) -> list[int]:
    """The voting-member template: leader + second + core of the target team,
    plus every member (any role) of the clergy team, deduped. No team named
    CLERGY_TEAM_NAME just means the second half contributes nobody."""
    stmt = sa.select(Membership.volunteer_id).where(
        Membership.team_id == team_id,
        Membership.role.in_([TeamRole.leader, TeamRole.second, TeamRole.core]),
    )
    volunteer_ids = list((await session.execute(stmt)).scalars())
    clergy = await session.execute(
        sa.select(Membership.volunteer_id)
        .join(Team, Team.id == Membership.team_id)
        .where(Team.name == CLERGY_TEAM_NAME)
    )
    volunteer_ids += list(clergy.scalars())
    return list(dict.fromkeys(volunteer_ids))


async def create_proposal(
    session: AsyncSession,
    actor: Actor | None,
    *,
    team_id: int,
    role: TeamRole,
    nomination_deadline: date,
    voting_deadline: date,
    created_by: int,
    candidates: Sequence[CandidateInput],
    notes: str | None = None,
    today: date | None = None,
) -> Proposal:
    """Open a proposal for the seat with its initial candidates and the
    template voting roll. A second OPEN proposal for the same (team, role)
    violates uq_proposal_open and surfaces as IntegrityError (409 / toast)."""
    require(
        actor is None or actor.can_manage_team(team_id),
        "open proposals for this team",
    )
    today = today or local_today()
    if not candidates:
        raise ValueError("at least one candidate is required")
    volunteer_ids = [c.volunteer_id for c in candidates]
    if len(set(volunteer_ids)) != len(volunteer_ids):
        raise ValueError("a volunteer can be a candidate only once")
    _check_deadlines(nomination_deadline, voting_deadline, today)

    proposal = Proposal(
        team_id=team_id,
        role=role,
        notes=(notes or "").strip() or None,
        nomination_deadline=nomination_deadline,
        voting_deadline=voting_deadline,
        created_by=created_by,
    )
    session.add(proposal)
    await session.flush()
    for c in candidates:
        session.add(
            ProposalCandidate(
                proposal_id=proposal.id,
                volunteer_id=c.volunteer_id,
                note=(c.note or "").strip() or None,
                nominated_by=created_by,
            )
        )
    for volunteer_id in await _default_roll(session, team_id):
        session.add(
            ProposalVoter(
                proposal_id=proposal.id,
                volunteer_id=volunteer_id,
                added_by=created_by,
            )
        )
    await session.flush()
    return proposal


async def update_proposal(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    nomination_deadline: date | None = None,
    voting_deadline: date | None = None,
    notes: str | None = _UNSET,  # type: ignore[assignment]
    today: date | None = None,
) -> Proposal:
    """Adjust deadlines/notes while open. Deadlines may move either way —
    pulling the voting deadline into the past concludes voting early,
    extending it past today reopens it — but nominations cannot reopen once
    ballots exist (the candidate set is frozen under cast ballots)."""
    today = today or local_today()
    proposal = await _managed(session, actor, proposal_id)
    if proposal.status != ProposalStatus.open.value:
        raise ValueError(f"proposal already {proposal.status}")
    new_d1 = nomination_deadline or proposal.nomination_deadline
    new_d2 = voting_deadline or proposal.voting_deadline
    if new_d2 <= new_d1:
        raise ValueError("voting deadline must fall after the nomination deadline")
    if new_d1 >= today and await _has_ballots(session, proposal_id):
        raise ValueError("nominations cannot reopen once ballots are cast")
    proposal.nomination_deadline = new_d1
    proposal.voting_deadline = new_d2
    if notes is not _UNSET:
        proposal.notes = (notes or "").strip() or None
    await session.flush()
    return proposal


async def _has_ballots(session: AsyncSession, proposal_id: int) -> bool:
    return (
        await session.execute(
            sa.select(ProposalBallot.id)
            .where(ProposalBallot.proposal_id == proposal_id)
            .limit(1)
        )
    ).first() is not None


async def add_candidate(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    volunteer_id: int,
    nominated_by: int,
    note: str | None = None,
    today: date | None = None,
) -> ProposalCandidate:
    """Nominate during the nominating phase. A duplicate (proposal, volunteer)
    violates uq_proposal_candidate and surfaces as IntegrityError.

    Wider than the other roll edits: a manager OR anybody on this proposal's
    voting roll may nominate, because putting a name forward is what the roll
    exists to do. Removing one stays with the managers."""
    today = today or local_today()
    proposal = await _get_or_raise(session, proposal_id)
    require(
        actor is None
        or actor.can_manage_team(proposal.team_id)
        or proposal_id in actor.voter_proposal_ids,
        "nominate on this proposal",
    )
    _require_phase(proposal, today, ProposalPhase.nominating, "nominate")
    candidate = ProposalCandidate(
        proposal_id=proposal_id,
        volunteer_id=volunteer_id,
        note=(note or "").strip() or None,
        nominated_by=nominated_by,
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def remove_candidate(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    candidate_id: int,
    *,
    today: date | None = None,
) -> None:
    today = today or local_today()
    proposal = await _managed(session, actor, proposal_id)
    _require_phase(proposal, today, ProposalPhase.nominating, "remove a candidate")
    candidate = await session.get(ProposalCandidate, candidate_id)
    if candidate is None or candidate.proposal_id != proposal_id:
        raise LookupError(f"candidate {candidate_id} not found")
    await session.delete(candidate)
    await session.flush()


async def add_voter(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    volunteer_id: int,
    added_by: int,
    today: date | None = None,
) -> ProposalVoter:
    """Roll edits are allowed only while nominating: the roll freezes when
    voting begins. Duplicates violate uq_proposal_voter (IntegrityError)."""
    today = today or local_today()
    proposal = await _managed(session, actor, proposal_id)
    _require_phase(proposal, today, ProposalPhase.nominating, "add a voter")
    voter = ProposalVoter(
        proposal_id=proposal_id, volunteer_id=volunteer_id, added_by=added_by
    )
    session.add(voter)
    await session.flush()
    return voter


async def remove_voter(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    voter_id: int,
    *,
    today: date | None = None,
) -> None:
    today = today or local_today()
    proposal = await _managed(session, actor, proposal_id)
    _require_phase(proposal, today, ProposalPhase.nominating, "remove a voter")
    voter = await session.get(ProposalVoter, voter_id)
    if voter is None or voter.proposal_id != proposal_id:
        raise LookupError(f"voter {voter_id} not found")
    await session.delete(voter)
    await session.flush()


# --- voting ------------------------------------------------------------------


async def cast_ballot(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    voter_volunteer_id: int,
    scores: Mapping[int, int],
    today: date | None = None,
) -> None:
    """Write the voter's whole ballot: one row per candidate, missing keys
    scored 0, revisable until the voting deadline (upsert).

    Only for the caller's OWN seat on the roll: `voter_volunteer_id` is checked
    against the actor rather than trusted, so no caller — however privileged —
    can cast somebody else's ballot. Ballots are secret; a manager who could
    write one could also read it back by writing it."""
    require(
        actor is None
        or (
            actor.volunteer_id == voter_volunteer_id
            and proposal_id in actor.voter_proposal_ids
        ),
        "vote on this proposal",
    )
    today = today or local_today()
    proposal = await _get_or_raise(session, proposal_id)
    _require_phase(proposal, today, ProposalPhase.voting, "vote")
    voter = (
        await session.execute(
            sa.select(ProposalVoter).where(
                ProposalVoter.proposal_id == proposal_id,
                ProposalVoter.volunteer_id == voter_volunteer_id,
            )
        )
    ).scalar_one_or_none()
    if voter is None:
        raise LookupError("not on this proposal's voting roll")
    candidate_ids = list(
        (
            await session.execute(
                sa.select(ProposalCandidate.id).where(
                    ProposalCandidate.proposal_id == proposal_id
                )
            )
        ).scalars()
    )
    if not candidate_ids:
        raise ValueError("proposal has no candidates")
    if unknown := set(scores) - set(candidate_ids):
        raise ValueError(f"not candidates of this proposal: {sorted(unknown)}")
    if any(not (0 <= s <= 5) for s in scores.values()):
        raise ValueError("scores must be between 0 and 5")
    stmt = pg_insert(ProposalBallot).values(
        [
            {
                "proposal_id": proposal_id,
                "voter_id": voter.id,
                "candidate_id": candidate_id,
                "score": int(scores.get(candidate_id, 0)),
            }
            for candidate_id in candidate_ids
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProposalBallot.voter_id, ProposalBallot.candidate_id],
        set_={"score": stmt.excluded.score, "updated_at": sa.func.now()},
    )
    await session.execute(stmt)


async def my_scores(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    voter_volunteer_id: int,
) -> dict[int, int]:
    """The voter's own scores, candidate id -> 0-5 (a ballot is not secret
    to its owner); empty if they have not voted.

    Only your own, and the id is checked against the actor rather than trusted:
    this is the one function that returns individual scores, so the whole
    secrecy guarantee rests on it."""
    require(
        actor is None or actor.volunteer_id == voter_volunteer_id,
        "read this ballot",
    )
    rows = await session.execute(
        sa.select(ProposalBallot.candidate_id, ProposalBallot.score)
        .join(ProposalVoter, ProposalVoter.id == ProposalBallot.voter_id)
        .where(
            ProposalBallot.proposal_id == proposal_id,
            ProposalVoter.volunteer_id == voter_volunteer_id,
        )
    )
    return dict(rows.all())


def _tally_visible(proposal: Proposal, today: date) -> bool:
    """Aggregates stay hidden while ballots may still change: they show only
    once the voting window has passed (or the proposal was appointed)."""
    return (
        proposal.status == ProposalStatus.appointed.value
        or today > proposal.voting_deadline
    )


async def _tally(session: AsyncSession, proposal_id: int) -> StarResult:
    candidate_ids = list(
        (
            await session.execute(
                sa.select(ProposalCandidate.id)
                .where(ProposalCandidate.proposal_id == proposal_id)
                .order_by(ProposalCandidate.id)
            )
        ).scalars()
    )
    rows = await session.execute(
        sa.select(
            ProposalBallot.voter_id, ProposalBallot.candidate_id, ProposalBallot.score
        ).where(ProposalBallot.proposal_id == proposal_id)
    )
    ballots: dict[int, dict[int, int]] = {}
    for voter_id, candidate_id, score in rows:
        ballots.setdefault(voter_id, {})[candidate_id] = score
    return star_tally(candidate_ids, list(ballots.values()))


# --- decisions ---------------------------------------------------------------


async def appoint(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    candidate_id: int,
    *,
    decided_by: int,
    today: date | None = None,
) -> Proposal:
    """Appoint a candidate once voting has concluded AND create the
    membership — both flush in the caller's session, so they commit (or
    roll back) together. assign() upserts: a volunteer already on the team
    gets their role updated. The tally is advisory: any candidate may be
    appointed, not just the STAR winner."""
    today = today or local_today()
    proposal = await _managed(session, actor, proposal_id)
    _require_phase(proposal, today, ProposalPhase.concluded, "appoint")
    candidate = await session.get(ProposalCandidate, candidate_id)
    if candidate is None or candidate.proposal_id != proposal_id:
        raise LookupError(f"candidate {candidate_id} not found")
    proposal.status = ProposalStatus.appointed.value
    proposal.appointed_candidate_id = candidate.id
    proposal.decided_by = decided_by
    proposal.decided_at = datetime.now(UTC)
    await membership_service.assign(
        # None: the caller of appoint() holds manage rights on this seat's team,
        # which is the same right the membership write would ask for
        session,
        None,
        candidate.volunteer_id,
        proposal.team_id,
        TeamRole(proposal.role),
    )
    await session.flush()
    return proposal


async def cancel(
    session: AsyncSession, actor: Actor | None, proposal_id: int, *, decided_by: int
) -> Proposal:
    proposal = await _managed(session, actor, proposal_id)
    if proposal.status != ProposalStatus.open.value:
        raise ValueError(f"proposal already {proposal.status}")
    proposal.status = ProposalStatus.cancelled.value
    proposal.decided_by = decided_by
    proposal.decided_at = datetime.now(UTC)
    await session.flush()
    return proposal


async def new_round(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    created_by: int,
    nomination_deadline: date,
    voting_deadline: date,
    today: date | None = None,
) -> Proposal:
    """The Ignatian "debate together, then repeat": close a concluded round
    and open a fresh one for the same seat, cloning candidates (notes and
    nominators travel) and the roll — never the ballots."""
    today = today or local_today()
    source = await _managed(session, actor, proposal_id)
    _require_phase(source, today, ProposalPhase.concluded, "start a new round")
    _check_deadlines(nomination_deadline, voting_deadline, today)

    source.status = ProposalStatus.cancelled.value
    source.decided_by = created_by
    source.decided_at = datetime.now(UTC)
    await session.flush()  # frees uq_proposal_open for the fresh round

    fresh = Proposal(
        team_id=source.team_id,
        role=TeamRole(source.role),
        notes=source.notes,
        nomination_deadline=nomination_deadline,
        voting_deadline=voting_deadline,
        created_by=created_by,
    )
    session.add(fresh)
    await session.flush()
    candidates = await session.execute(
        sa.select(ProposalCandidate).where(ProposalCandidate.proposal_id == source.id)
    )
    for c in candidates.scalars():
        session.add(
            ProposalCandidate(
                proposal_id=fresh.id,
                volunteer_id=c.volunteer_id,
                note=c.note,
                nominated_by=c.nominated_by,
            )
        )
    voters = await session.execute(
        sa.select(ProposalVoter).where(ProposalVoter.proposal_id == source.id)
    )
    for v in voters.scalars():
        session.add(
            ProposalVoter(
                proposal_id=fresh.id, volunteer_id=v.volunteer_id, added_by=v.added_by
            )
        )
    await session.flush()
    return fresh
