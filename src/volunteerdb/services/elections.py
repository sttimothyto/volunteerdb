"""Elections: fill a (team, role) seat by nomination and STAR vote.

A vacancy is a team missing a leader or a second-in-command (derived from
the coverage report, never stored). A manager opens a proposal for the
seat; candidates are nominated — each with a "why them" note — until the
nomination deadline, then the voting roll scores every candidate 0-5 until
the voting deadline (STAR, see star.py), then a manager appoints or starts
a new round: the Ignatian rhythm of pray, vote, debate, repeat. The phase
of an open proposal derives from today's date in the parish timezone;
nothing runs on a clock -- `today` is the parish day the edge read, and
`now` the moment it read it, both parameters.

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
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import DomainError, Invalid, NotFound, invalid, not_found, require
from ..fp import Err, Ok, Result
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
from ..permissions import Actor
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


def phase_of_dates(
    nomination_deadline: date, voting_deadline: date, today: date
) -> ProposalPhase:
    """An open proposal's phase from its deadlines. Inclusive: the seat
    nominates through the end of d1 and votes through the end of d2."""
    if today <= nomination_deadline:
        return ProposalPhase.nominating
    if today <= voting_deadline:
        return ProposalPhase.voting
    return ProposalPhase.concluded


def phase_of(proposal: Proposal, today: date) -> ProposalPhase | None:
    """The open proposal's phase; None once decided."""
    if proposal.status != ProposalStatus.open.value:
        return None
    return phase_of_dates(proposal.nomination_deadline, proposal.voting_deadline, today)


def _require_phase(
    proposal: Proposal, today: date, expected: ProposalPhase, action: str
) -> Err[Invalid] | None:
    phase = phase_of(proposal, today)
    if phase != expected:
        state = phase.value if phase else proposal.status
        return invalid(f"cannot {action}: proposal is {state}, not {expected.value}")
    return None


def _check_deadlines(
    nomination_deadline: date, voting_deadline: date, today: date
) -> Err[Invalid] | None:
    if nomination_deadline < today:
        return invalid("nomination deadline is in the past")
    if voting_deadline <= nomination_deadline:
        return invalid("voting deadline must fall after the nomination deadline")
    return None


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
) -> Result[Proposal, DomainError]:
    """The proposal, for a caller who runs the seat's team.

    Managing a proposal — deadlines, the roll, the candidate list, appointing,
    cancelling, starting a round — is the same right as editing that team's
    roster, because appointing *is* a roster write."""
    found = await _get(session, proposal_id)
    if isinstance(found, Err):
        return found
    if denied := require(
        actor is None or actor.can_manage_team(found.value.team_id),
        "manage proposals for this team",
    ):
        return denied
    return found


async def _viewable(
    session: AsyncSession, actor: Actor | None, proposal_id: int
) -> Result[Proposal, DomainError]:
    """The proposal, for a manager of its team or somebody on its roll. Voters
    keep access after the decision, to see the result they voted on."""
    found = await _get(session, proposal_id)
    if isinstance(found, Err):
        return found
    if denied := require(
        actor is None or actor.can_view_proposal(proposal_id, found.value.team_id),
        "view this proposal",
    ):
        return denied
    return found


async def _get(session: AsyncSession, proposal_id: int) -> Result[Proposal, NotFound]:
    proposal = await get(session, proposal_id)
    if proposal is None:
        return not_found("proposal", proposal_id)
    return Ok(proposal)


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
    today: date,
) -> list[ProposalSummary]:
    """Proposals joined for display, newest first. Admins see all; everyone
    else sees their managed subtree plus any roll they sit on. Live-only:
    an election is about now, so there is no as-of variant."""
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
    today: date,
) -> list[ProposalInvolvement]:
    """Proposals where the volunteer is a candidate or sits on the voting
    roll, newest first, scoped exactly like list_proposals — so a nominee
    without elections access never learns of their own nomination."""
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
    today: date,
) -> Result[ProposalDetail, DomainError]:
    shown = await _viewable(session, actor, proposal_id)
    if isinstance(shown, Err):
        return shown
    proposal = shown.value
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

    return Ok(
        ProposalDetail(
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
    today: date,
) -> Result[Proposal, DomainError]:
    """Open a proposal for the seat with its initial candidates and the
    template voting roll. A second OPEN proposal for the same (team, role)
    violates uq_proposal_open and surfaces as IntegrityError (409 / toast)."""
    if denied := require(
        actor is None or actor.can_manage_team(team_id),
        "open proposals for this team",
    ):
        return denied
    if not candidates:
        return invalid("at least one candidate is required")
    volunteer_ids = [c.volunteer_id for c in candidates]
    if len(set(volunteer_ids)) != len(volunteer_ids):
        return invalid("a volunteer can be a candidate only once")
    if bad := _check_deadlines(nomination_deadline, voting_deadline, today):
        return bad

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
    return Ok(proposal)


async def update_proposal(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    nomination_deadline: date | None = None,
    voting_deadline: date | None = None,
    notes: str | None = _UNSET,  # type: ignore[assignment]
    today: date,
) -> Result[Proposal, DomainError]:
    """Adjust deadlines/notes while open. Deadlines may move either way —
    pulling the voting deadline into the past concludes voting early,
    extending it past today reopens it — but nominations cannot reopen once
    ballots exist (the candidate set is frozen under cast ballots)."""
    managed = await _managed(session, actor, proposal_id)
    if isinstance(managed, Err):
        return managed
    proposal = managed.value
    if proposal.status != ProposalStatus.open.value:
        return invalid(f"proposal already {proposal.status}")
    new_d1 = nomination_deadline or proposal.nomination_deadline
    new_d2 = voting_deadline or proposal.voting_deadline
    if new_d2 <= new_d1:
        return invalid("voting deadline must fall after the nomination deadline")
    if new_d1 >= today and await _has_ballots(session, proposal_id):
        return invalid("nominations cannot reopen once ballots are cast")
    proposal.nomination_deadline = new_d1
    proposal.voting_deadline = new_d2
    if notes is not _UNSET:
        proposal.notes = (notes or "").strip() or None
    await session.flush()
    return Ok(proposal)


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
    today: date,
) -> Result[ProposalCandidate, DomainError]:
    """Nominate during the nominating phase. A duplicate (proposal, volunteer)
    violates uq_proposal_candidate and surfaces as IntegrityError.

    Wider than the other roll edits: a manager OR anybody on this proposal's
    voting roll may nominate, because putting a name forward is what the roll
    exists to do. Removing one stays with the managers."""
    found = await _get(session, proposal_id)
    if isinstance(found, Err):
        return found
    proposal = found.value
    if denied := require(
        actor is None
        or actor.can_manage_team(proposal.team_id)
        or proposal_id in actor.voter_proposal_ids,
        "nominate on this proposal",
    ):
        return denied
    if wrong := _require_phase(proposal, today, ProposalPhase.nominating, "nominate"):
        return wrong
    candidate = ProposalCandidate(
        proposal_id=proposal_id,
        volunteer_id=volunteer_id,
        note=(note or "").strip() or None,
        nominated_by=nominated_by,
    )
    session.add(candidate)
    await session.flush()
    return Ok(candidate)


async def remove_candidate(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    candidate_id: int,
    *,
    today: date,
) -> Result[None, DomainError]:
    managed = await _managed(session, actor, proposal_id)
    if isinstance(managed, Err):
        return managed
    if wrong := _require_phase(
        managed.value, today, ProposalPhase.nominating, "remove a candidate"
    ):
        return wrong
    candidate = await session.get(ProposalCandidate, candidate_id)
    if candidate is None or candidate.proposal_id != proposal_id:
        return not_found("candidate", candidate_id)
    await session.delete(candidate)
    await session.flush()
    return Ok(None)


async def add_voter(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    volunteer_id: int,
    added_by: int,
    today: date,
) -> Result[ProposalVoter, DomainError]:
    """Roll edits are allowed only while nominating: the roll freezes when
    voting begins. Duplicates violate uq_proposal_voter (IntegrityError)."""
    managed = await _managed(session, actor, proposal_id)
    if isinstance(managed, Err):
        return managed
    if wrong := _require_phase(
        managed.value, today, ProposalPhase.nominating, "add a voter"
    ):
        return wrong
    voter = ProposalVoter(
        proposal_id=proposal_id, volunteer_id=volunteer_id, added_by=added_by
    )
    session.add(voter)
    await session.flush()
    return Ok(voter)


async def remove_voter(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    voter_id: int,
    *,
    today: date,
) -> Result[None, DomainError]:
    managed = await _managed(session, actor, proposal_id)
    if isinstance(managed, Err):
        return managed
    if wrong := _require_phase(
        managed.value, today, ProposalPhase.nominating, "remove a voter"
    ):
        return wrong
    voter = await session.get(ProposalVoter, voter_id)
    if voter is None or voter.proposal_id != proposal_id:
        return not_found("voter", voter_id)
    await session.delete(voter)
    await session.flush()
    return Ok(None)


# --- voting ------------------------------------------------------------------


async def cast_ballot(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    voter_volunteer_id: int,
    scores: Mapping[int, int],
    today: date,
    now: datetime,
) -> Result[None, DomainError]:
    """Write the voter's whole ballot: one row per candidate, missing keys
    scored 0, revisable until the voting deadline (upsert).

    Only for the caller's OWN seat on the roll: `voter_volunteer_id` is checked
    against the actor rather than trusted, so no caller — however privileged —
    can cast somebody else's ballot. Ballots are secret; a manager who could
    write one could also read it back by writing it."""
    if denied := require(
        actor is None
        or (
            actor.volunteer_id == voter_volunteer_id
            and proposal_id in actor.voter_proposal_ids
        ),
        "vote on this proposal",
    ):
        return denied
    found = await _get(session, proposal_id)
    if isinstance(found, Err):
        return found
    if wrong := _require_phase(found.value, today, ProposalPhase.voting, "vote"):
        return wrong
    voter = (
        await session.execute(
            sa.select(ProposalVoter).where(
                ProposalVoter.proposal_id == proposal_id,
                ProposalVoter.volunteer_id == voter_volunteer_id,
            )
        )
    ).scalar_one_or_none()
    if voter is None:
        return not_found("seat on this proposal's voting roll")
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
        return invalid("proposal has no candidates")
    if unknown := set(scores) - set(candidate_ids):
        return invalid(f"not candidates of this proposal: {sorted(unknown)}")
    if any(not (0 <= s <= 5) for s in scores.values()):
        return invalid("scores must be between 0 and 5")
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
        set_={"score": stmt.excluded.score, "updated_at": now},
    )
    await session.execute(stmt)
    return Ok(None)


async def my_scores(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    voter_volunteer_id: int,
) -> Result[dict[int, int], DomainError]:
    """The voter's own scores, candidate id -> 0-5 (a ballot is not secret
    to its owner); empty if they have not voted.

    Only your own, and the id is checked against the actor rather than trusted:
    this is the one function that returns individual scores, so the whole
    secrecy guarantee rests on it."""
    if denied := require(
        actor is None or actor.volunteer_id == voter_volunteer_id,
        "read this ballot",
    ):
        return denied
    rows = await session.execute(
        sa.select(ProposalBallot.candidate_id, ProposalBallot.score)
        .join(ProposalVoter, ProposalVoter.id == ProposalBallot.voter_id)
        .where(
            ProposalBallot.proposal_id == proposal_id,
            ProposalVoter.volunteer_id == voter_volunteer_id,
        )
    )
    return Ok(dict(rows.all()))


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
    today: date,
    now: datetime,
) -> Result[Proposal, DomainError]:
    """Appoint a candidate once voting has concluded AND create the
    membership — both flush in the caller's session, so they commit (or
    roll back) together. assign() upserts: a volunteer already on the team
    gets their role updated. The tally is advisory: any candidate may be
    appointed, not just the STAR winner."""
    managed = await _managed(session, actor, proposal_id)
    if isinstance(managed, Err):
        return managed
    proposal = managed.value
    if wrong := _require_phase(proposal, today, ProposalPhase.concluded, "appoint"):
        return wrong
    candidate = await session.get(ProposalCandidate, candidate_id)
    if candidate is None or candidate.proposal_id != proposal_id:
        return not_found("candidate", candidate_id)
    proposal.status = ProposalStatus.appointed.value
    proposal.appointed_candidate_id = candidate.id
    proposal.decided_by = decided_by
    proposal.decided_at = now
    seated = await membership_service.assign(
        # None: the caller of appoint() holds manage rights on this seat's team,
        # which is the same right the membership write would ask for
        session,
        None,
        candidate.volunteer_id,
        proposal.team_id,
        TeamRole(proposal.role),
    )
    if isinstance(seated, Err):
        return seated
    await session.flush()
    return Ok(proposal)


async def cancel(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    decided_by: int,
    now: datetime,
) -> Result[Proposal, DomainError]:
    managed = await _managed(session, actor, proposal_id)
    if isinstance(managed, Err):
        return managed
    proposal = managed.value
    if proposal.status != ProposalStatus.open.value:
        return invalid(f"proposal already {proposal.status}")
    proposal.status = ProposalStatus.cancelled.value
    proposal.decided_by = decided_by
    proposal.decided_at = now
    await session.flush()
    return Ok(proposal)


async def new_round(
    session: AsyncSession,
    actor: Actor | None,
    proposal_id: int,
    *,
    created_by: int,
    nomination_deadline: date,
    voting_deadline: date,
    today: date,
    now: datetime,
) -> Result[Proposal, DomainError]:
    """The Ignatian "debate together, then repeat": close a concluded round
    and open a fresh one for the same seat, cloning candidates (notes and
    nominators travel) and the roll — never the ballots."""
    managed = await _managed(session, actor, proposal_id)
    if isinstance(managed, Err):
        return managed
    source = managed.value
    if wrong := _require_phase(
        source, today, ProposalPhase.concluded, "start a new round"
    ):
        return wrong
    if bad := _check_deadlines(nomination_deadline, voting_deadline, today):
        return bad

    source.status = ProposalStatus.cancelled.value
    source.decided_by = created_by
    source.decided_at = now
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
    return Ok(fresh)
