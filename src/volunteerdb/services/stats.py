"""Dashboard statistics, tiered by how wide the audience is.

Three tiers, computed independently: parish-wide numbers for admins, what
leadership needs to act on, and what is specific to the signed-in volunteer.

Access control is the shape of this module, not a filter over its output. A
tier — and each tile inside the leadership tier — is *computed only if the
actor may see it*, through the same `Actor` predicate that already gates the
page it links to. A plain member never runs the coverage or workload queries
at all, so there is nothing to leak. `None` means "not shown to this viewer,
or not applicable here"; a zero is a real, visible zero.

As-of: volunteer/team/membership counts, coverage and workload are versioned
and honour `at`. Events, elections, ballots, service hours and account counts
are live-only concepts (an election is about now; app_user is not versioned),
so under a snapshot they come back None and the page says why.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..fp import expect
from ..history import entity
from ..models import (
    AppUser,
    Membership,
    ProposalBallot,
    ProposalVoter,
    Volunteer,
)
from ..permissions import Actor, team_ids_map
from . import elections as election_service
from . import events as event_service
from . import reports as report_service
from . import teams as team_service
from . import workload as workload_service

# how far ahead "coming up" looks, for events
HORIZON = timedelta(days=30)
# how many teams the leadership gap tile names before it stops listing
GAP_SAMPLE = 5


@dataclass(frozen=True)
class ParishStats:
    """The whole parish, for admins only."""

    active_volunteers: int
    inactive_volunteers: int
    active_teams: int
    assignments: int  # membership rows: one person on three teams counts three
    unassigned_volunteers: int  # active, but on no team at all
    accounts: int | None  # active volunteers who can sign in; None as-of

    @property
    def ministries_per_volunteer(self) -> float:
        if not self.active_volunteers:
            return 0.0
        return round(self.assignments / self.active_volunteers, 1)


@dataclass(frozen=True)
class GapTeam:
    team_id: int
    path: str
    missing_leader: bool
    missing_second: bool


@dataclass(frozen=True)
class BandCount:
    label: str
    color: str
    count: int


@dataclass(frozen=True)
class PhaseCount:
    phase: str
    label: str
    count: int


@dataclass(frozen=True)
class LeadershipStats:
    """What the people who run ministries need to act on.

    The first three fields are the reach of the actor's full-view scope and
    are the tile a core team member sees; everything below them carries its
    own predicate and is None when that predicate says no.
    """

    teams: int
    people: int
    people_without_email: int

    # leadership gaps — admin, or leader/second of the team (as on /teams)
    teams_without_leader: int | None = None
    teams_without_second: int | None = None
    gap_teams: tuple[GapTeam, ...] = ()

    # workload spread — admin, or leader/second; never core, never oneself
    bands: tuple[BandCount, ...] | None = None

    # live-only below here
    understaffed_events: int | None = None
    open_elections: tuple[PhaseCount, ...] | None = None


@dataclass(frozen=True)
class PersonalStats:
    """The signed-in volunteer's own service. Live-only.

    Deliberately no workload band: `Actor.can_view_workload` excludes the
    volunteer themself, and that is a documented promise, not an oversight.
    """

    upcoming_duties: int
    next_duty_at: datetime | None
    next_duty_title: str | None
    next_duty_slot: str | None
    claimable_subs: int
    ballots_waiting: int
    hours_served: Decimal
    events_attended: int


@dataclass(frozen=True)
class DashboardStats:
    parish: ParishStats | None
    leadership: LeadershipStats | None
    personal: PersonalStats | None
    live: bool  # False when read as-of, so live-only tiles were skipped


async def dashboard(
    session: AsyncSession,
    actor: Actor,
    *,
    at: datetime | None = None,
    now: datetime,
    today: date,
) -> DashboardStats:
    """Every statistic `actor` may see, and nothing else. `now` is the
    edge's clock read: the upcoming window and the service record hang off it."""
    live = at is None

    parish = await _parish(session, at=at) if actor.is_admin else None
    leadership = (
        await _leadership(session, actor, at=at, now=now, today=today)
        if actor.is_admin or actor.full_view_team_ids
        else None
    )
    personal = (
        await _personal(session, actor, now=now, today=today)
        if live and actor.volunteer_id is not None
        else None
    )
    return DashboardStats(
        parish=parish, leadership=leadership, personal=personal, live=live
    )


async def _parish(session: AsyncSession, *, at: datetime | None) -> ParishStats:
    V, M = entity(Volunteer, at), entity(Membership, at)

    by_active = dict(
        (
            await session.execute(
                sa.select(V.is_active, sa.func.count()).group_by(V.is_active)
            )
        ).all()
    )
    # joined to the volunteer rather than counted bare: live this changes
    # nothing (every membership has one), but a snapshot can hold a membership
    # whose volunteer row is not in it yet, and "3 assignments among 0 people"
    # is not a fact about the parish
    assignments = (
        await session.execute(
            sa.select(sa.func.count()).select_from(M).join(V, V.id == M.volunteer_id)
        )
    ).scalar_one()
    # active volunteers holding at least one membership; the difference from
    # the active headcount is the "on no team at all" tile, which is the one
    # an admin can actually do something about
    assigned = (
        await session.execute(
            sa.select(sa.func.count(sa.distinct(M.volunteer_id)))
            .select_from(M)
            .join(V, V.id == M.volunteer_id)
            .where(V.is_active.is_(True))
        )
    ).scalar_one()

    accounts = None
    if at is None:  # app_user is not system-versioned, so there is no as-of answer
        accounts = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(AppUser)
                .where(AppUser.volunteer_id.is_not(None), AppUser.is_active.is_(True))
            )
        ).scalar_one()

    active = by_active.get(True, 0)
    teams = (await team_service.tree(session, at)).teams
    return ParishStats(
        active_volunteers=active,
        inactive_volunteers=by_active.get(False, 0),
        active_teams=sum(1 for t in teams if t.is_active),
        assignments=assignments,
        unassigned_volunteers=max(active - assigned, 0),
        accounts=accounts,
    )


async def _leadership(
    session: AsyncSession,
    actor: Actor,
    *,
    at: datetime | None,
    now: datetime,
    today: date,
) -> LeadershipStats:
    # None means "no team filter": an admin's scope is the whole parish, and
    # their managed/full-view sets are empty precisely because they need none
    view_scope = None if actor.is_admin else actor.full_view_team_ids
    manage_scope = None if actor.is_admin else actor.managed_team_ids

    V, M = entity(Volunteer, at), entity(Membership, at)
    reach_stmt = (
        sa.select(
            sa.func.count(sa.distinct(M.volunteer_id)),
            # count() ignores NULLs, so the CASE with no ELSE counts exactly
            # the people who cannot be emailed an invite or an event notice
            sa.func.count(sa.distinct(sa.case((V.email.is_(None), M.volunteer_id)))),
        )
        .select_from(M)
        .join(V, V.id == M.volunteer_id)
        .where(V.is_active.is_(True))
    )
    if view_scope is not None:
        reach_stmt = reach_stmt.where(M.team_id.in_(view_scope))
    people, people_without_email = (await session.execute(reach_stmt)).one()

    teams = (await team_service.tree(session, at)).teams
    visible_teams = [
        t for t in teams if t.is_active and (view_scope is None or t.id in view_scope)
    ]
    stats = LeadershipStats(
        teams=len(visible_teams),
        people=people,
        people_without_email=people_without_email,
    )

    # --- leadership gaps: the same gate as GET /api/reports/coverage --------
    if actor.is_admin or actor.managed_team_ids:
        rows = await report_service.coverage(session, at)
        if manage_scope is not None:
            rows = [r for r in rows if r.team.id in manage_scope]
        # coverage() already sorts holes first, so the sample is the worst few
        gaps = [r for r in rows if r.missing_leader or r.missing_second]
        stats = replace(
            stats,
            teams_without_leader=sum(1 for r in rows if r.missing_leader),
            teams_without_second=sum(1 for r in rows if r.missing_second),
            gap_teams=tuple(
                GapTeam(
                    team_id=r.team.id,
                    path=r.path,
                    missing_leader=r.missing_leader,
                    missing_second=r.missing_second,
                )
                for r in gaps[:GAP_SAMPLE]
            ),
        )

        # --- workload spread ------------------------------------------------
        # visible_scores applies can_view_workload per volunteer, so this is
        # the same permission the graph's coloured dots run through — and the
        # same query pair. Kept independent of the graph on purpose: these
        # numbers must not move when someone narrows the graph to one team.
        candidates_stmt = sa.select(sa.distinct(M.volunteer_id)).select_from(M)
        if manage_scope is not None:
            candidates_stmt = candidates_stmt.where(M.team_id.in_(manage_scope))
        candidates = list((await session.execute(candidates_stmt)).scalars())
        tally: dict[str, int] = {}
        if candidates:
            team_sets = await team_ids_map(session, candidates, at=at)
            scored = await workload_service.visible_scores(
                session, actor, team_sets, at=at
            )
            for _score, band in scored.values():
                tally[band.label] = tally.get(band.label, 0) + 1
        config = await workload_service.read_config(session)
        stats = replace(
            stats,
            bands=tuple(
                BandCount(label=b.label, color=b.color, count=tally.get(b.label, 0))
                for b in config.bands
            ),
        )

    if at is not None:
        return stats  # everything below is about now

    # --- shifts that still need people --------------------------------------
    if actor.can_create_events:
        upcoming = await event_service.list_events(
            session, actor, from_=now, to=now + HORIZON
        )
        stats = replace(
            stats,
            understaffed_events=sum(
                1 for e in upcoming if e.capacity is not None and e.filled < e.capacity
            ),
        )

    # --- seats being filled --------------------------------------------------
    if actor.can_access_elections:
        # list_proposals scopes itself to managed teams plus the rolls the
        # actor sits on, so no further filtering is needed here
        proposals = await election_service.list_proposals(
            session, actor, status="open", today=today
        )
        counts: dict[str, int] = {}
        for p in proposals:
            if p.phase is not None:
                counts[p.phase.value] = counts.get(p.phase.value, 0) + 1
        stats = replace(
            stats,
            open_elections=tuple(
                PhaseCount(
                    phase=phase.value,
                    label=election_service.PHASE_LABELS[phase],
                    count=counts.get(phase.value, 0),
                )
                for phase in election_service.ProposalPhase
                if counts.get(phase.value)
            ),
        )
    return stats


async def _personal(
    session: AsyncSession, actor: Actor, *, now: datetime, today: date
) -> PersonalStats:
    volunteer_id = actor.volunteer_id
    assert volunteer_id is not None  # guarded by the caller

    duties = await event_service.my_upcoming(session, volunteer_id, now=now)
    subs = await event_service.claimable_subs(session, actor, now=now)
    # None: _personal only ever reports the actor's OWN service record
    hours = expect(
        await event_service.hours_for_volunteer(session, None, volunteer_id, now=now)
    )

    # ballots the actor still owes: proposals they sit on the roll for, in the
    # voting phase, with nothing cast yet. involving() already scopes to what
    # they may see, so a roll they cannot read cannot appear here either.
    involvements = await election_service.involving(
        session, actor, volunteer_id, today=today
    )
    votable = {
        i.proposal.id
        for i in involvements
        if i.as_voter and i.phase == election_service.ProposalPhase.voting
    }
    ballots_waiting = 0
    if votable:
        # ballot.voter_id points at proposal_voter.id, not at a volunteer, so
        # the roll row is the join (the same hop as elections.my_ballot)
        voted = set(
            (
                await session.execute(
                    sa.select(sa.distinct(ProposalBallot.proposal_id))
                    .join(ProposalVoter, ProposalVoter.id == ProposalBallot.voter_id)
                    .where(
                        ProposalBallot.proposal_id.in_(votable),
                        ProposalVoter.volunteer_id == volunteer_id,
                    )
                )
            ).scalars()
        )
        ballots_waiting = len(votable - voted)

    nxt = duties[0] if duties else None
    return PersonalStats(
        upcoming_duties=len(duties),
        next_duty_at=nxt.event.starts_at if nxt else None,
        next_duty_title=nxt.event.title if nxt else None,
        next_duty_slot=nxt.slot.name if nxt else None,
        claimable_subs=len(subs),
        ballots_waiting=ballots_waiting,
        hours_served=hours.total_hours,
        events_attended=hours.events_attended,
    )
