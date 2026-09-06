"""What a detail page shows, read once.

The page for one event, one team, one volunteer or one proposal is a header,
a list and a handful of sections, and which sections exist depends on who is
reading: a manager sees the attendance sheet, a member the sign-up buttons,
a voter the ballot. The four readers here answer both questions in one call
-- the rows, and the facts about the reader that decide the sections -- as a
frozen value the page then draws top to bottom. The JSON API's event detail
is the same value with fewer sections drawn.

Each takes the session, the actor and the moment (`now`, with the parish
zone where a date is needed). The refusal comes from the gated reader each
one opens with -- events.detail, elections.detail, teams.roster and the
volunteer's own gated readers -- so these decide which sections exist for
the actor, never whether the page exists for them.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import DomainError, NotFound, not_found
from ..fp import Err, Ok, Result
from ..models import (
    AppUser,
    CustomFieldDef,
    Event,
    EventAssignment,
    EventRsvp,
    EventSlot,
    EventStatus,
    EventSubRequest,
    Membership,
    Proposal,
    Team,
    TeamPage,
    TeamSheet,
    Volunteer,
)
from ..permissions import Actor, team_ids_map, volunteer_team_ids
from . import custom_fields as custom_field_service
from . import elections as elections_service
from . import events as event_service
from . import pages as page_service
from . import photos as photo_service
from . import task_force as task_force_service
from . import teams as team_service
from . import users as user_service
from . import volunteers as volunteer_service
from . import workload as workload_service
from .workload import Band

# --- one event -----------------------------------------------------------------


@dataclass(frozen=True)
class EventWorkroom:
    """/events/{id}: the event with its slots, and what this reader may do
    there. `roster` is the team's roster for the pickers, loaded only for a
    manager or somebody holding a slot (the hand-off picker shows names);
    `attendance` is the sheet, and None unless it is the manager's to see."""

    view: event_service.EventDetail
    my_volunteer_id: int | None
    can_manage: bool
    am_member: bool
    upcoming: bool
    roster: list[tuple[Membership, Volunteer]]
    attendance: list[tuple[EventAssignment, EventSlot, Volunteer]] | None
    task_force: task_force_service.TaskForceView | None
    source_paths: list[str]  # the teams staffing a task force, as display paths
    collaborator_options: dict[int, str]  # teams a manager could add

    @property
    def event(self) -> Event:
        return self.view.event

    @property
    def series(self) -> bool:
        return self.event.series_id is not None

    @property
    def rsvp_by_volunteer(self) -> dict[int, EventRsvp]:
        return {v.id: r for r, v in self.view.rsvps}

    @property
    def my_rsvp(self) -> EventRsvp | None:
        return self.rsvp_by_volunteer.get(self.my_volunteer_id)

    @property
    def my_assignment(self) -> EventAssignment | None:
        return next(
            (
                a
                for sv in self.view.slots
                for a, v in sv.entries
                if v.id == self.my_volunteer_id
            ),
            None,
        )

    @property
    def assigned_ids(self) -> set[int]:
        return {v.id for sv in self.view.slots for _, v in sv.entries}

    @property
    def sub_wanted(self) -> dict[int, EventSubRequest]:
        """assignment id -> the open substitution call on it."""
        return {a.id: sub for sub, a in self.view.open_subs}

    @property
    def claimable_subs(self) -> list[tuple[EventSubRequest, EventAssignment]]:
        """The open calls this reader could answer: a member of the team, not
        already serving at the event, and never their own call."""
        me = self.my_volunteer_id
        if not (self.am_member and self.upcoming) or me in self.assigned_ids:
            return []
        return [(sub, a) for sub, a in self.view.open_subs if a.volunteer_id != me]

    def picker_options(self) -> dict[int, str]:
        """Who could be scheduled: the roster minus everyone already on a
        slot, those who said they are available first, those who said they
        are not last and marked."""
        rsvps = self.rsvp_by_volunteer

        def rank(volunteer_id: int) -> int:
            rsvp = rsvps.get(volunteer_id)
            if rsvp is None:
                return 1
            return 0 if rsvp.available else 2

        assigned = self.assigned_ids
        entries = sorted(
            (v for _, v in self.roster if v.id not in assigned),
            key=lambda v: (rank(v.id), v.last_name, v.first_name),
        )
        suffix = {0: " · available", 1: "", 2: " · UNAVAILABLE"}
        return {v.id: f"{v.full_name}{suffix[rank(v.id)]}" for v in entries}


async def event_workroom(
    session: AsyncSession, actor: Actor, event_id: int, *, now: datetime
) -> Result[EventWorkroom, DomainError]:
    shown = await event_service.detail(session, actor, event_id)
    if isinstance(shown, Err):
        return shown
    view = shown.value
    event = view.event
    me = actor.volunteer_id
    can_manage = actor.can_manage_team(event.team_id)
    am_member = me is not None and await event_service.is_member(
        session, me, event.team_id
    )
    holds_a_slot = any(v.id == me for sv in view.slots for _, v in sv.entries)
    upcoming = event.status == EventStatus.scheduled.value and not (
        event_service.is_past(event, now=now)
    )

    roster: list[tuple[Membership, Volunteer]] = []
    if can_manage or holds_a_slot:
        rows = await team_service.roster(session, actor, event.team_id)
        if isinstance(rows, Err):
            return rows
        roster = rows.value

    attendance = None
    if can_manage and not upcoming and event.status == EventStatus.scheduled.value:
        sheet = await event_service.attendance_rows(session, actor, event_id)
        if isinstance(sheet, Err):
            return sheet
        attendance = sheet.value

    task_force = None
    source_paths: list[str] = []
    collaborator_options: dict[int, str] = {}
    if can_manage:
        task_force = await task_force_service.get_for_event(session, event_id)
        tree = await team_service.tree(session)
        staffing = (
            {t.id for t in task_force.sources} | {task_force.team_id}
            if task_force
            else {event.team_id}
        )
        collaborator_options = {
            t.id: tree.paths[t.id]
            for t in tree.teams
            if t.is_active and t.id not in staffing
        }
        if task_force:
            source_paths = [tree.paths.get(t.id, t.name) for t in task_force.sources]

    return Ok(
        EventWorkroom(
            view=view,
            my_volunteer_id=me,
            can_manage=can_manage,
            am_member=am_member,
            upcoming=upcoming,
            roster=roster,
            attendance=attendance,
            task_force=task_force,
            source_paths=source_paths,
            collaborator_options=collaborator_options,
        )
    )


# --- one team ------------------------------------------------------------------


@dataclass(frozen=True)
class TeamRoom:
    """/teams/{id}, live or as of a past instant. The three rights are the
    roster's tiers (names, full details, management); `can_manage` and
    `can_invite` are False on a snapshot, where the roster is history."""

    team: Team
    path: str
    at: datetime | None  # the snapshot instant, or None for the live page
    tree: team_service.TeamTree
    children: list[Team]
    slug: str | None  # the public page's address, if the team has one
    can_names: bool
    can_full: bool
    can_manage: bool
    can_invite: bool
    roster: list[tuple[Membership, Volunteer]]
    accounts: dict[int, AppUser]  # volunteer id -> sign-in account
    volunteer_options: dict[int, str]  # for the add-member picker
    page: TeamPage | None  # the public page's fetch status, for editors
    has_public_page: bool
    sheet: TeamSheet | None  # the roster spreadsheet, for managers
    anniversaries: list[volunteer_service.Anniversary]
    upcoming_events: list[event_service.EventSummary]

    @property
    def live(self) -> bool:
        return self.at is None

    @property
    def emails(self) -> list[str]:
        """The roster's addresses, deduplicated and sorted."""
        return sorted({v.email for _, v in self.roster if v.email})


async def team_room(
    session: AsyncSession,
    actor: Actor,
    team_id: int,
    *,
    now: datetime,
    tz: ZoneInfo,
    at: datetime | None = None,
) -> Result[TeamRoom, NotFound]:
    # out of the tree rather than teams.get(): the page reads the whole
    # table either way, and get() is a second round trip for a row in hand
    tree = await team_service.tree(session, at=at)
    team = tree.by_id.get(team_id)
    if team is None:
        return not_found("team", team_id)
    live = at is None
    can_names = actor.can_view_roster_names(team_id)
    can_full = actor.can_view_full_roster(team_id)
    can_manage = actor.can_manage_team(team_id) and live

    roster: list[tuple[Membership, Volunteer]] = []
    if can_names:
        rows = await team_service.roster(session, actor, team_id, at=at)
        roster = rows.value if isinstance(rows, Ok) else []
    # accounts are not system-versioned (like photos): an as-of roster still
    # reports who can sign in *now*
    accounts = await user_service.accounts_by_volunteer(
        session, [v.id for _, v in roster]
    )
    page = None
    if can_full and live:
        status = await page_service.page_status(session, actor, team_id)
        page = status.value if isinstance(status, Ok) else None
    sheet = None
    if can_manage:
        linked = await team_service.roster_sheet(session, actor, team_id)
        sheet = linked.value if isinstance(linked, Ok) else None
    slug = page_service.slug_map(tree.paths).get(team_id)
    return Ok(
        TeamRoom(
            team=team,
            path=tree.paths.get(team_id, team.name),
            at=at,
            tree=tree,
            children=tree.by_parent.get(team_id, []),
            slug=slug,
            can_names=can_names,
            can_full=can_full,
            can_manage=can_manage,
            # leader/second/core of this team may invite its members; never
            # off a snapshot, where the addresses may be stale
            can_invite=can_full and live,
            roster=roster,
            accounts=accounts,
            volunteer_options=(
                await volunteer_service.name_map(session) if can_manage else {}
            ),
            page=page,
            # whose roster you are on has nothing to do with a page the world
            # can read; this never pulls the html the way `page` does
            has_public_page=(
                slug is not None and await page_service.is_published(session, team_id)
            ),
            sheet=sheet,
            anniversaries=(
                await volunteer_service.team_anniversaries(
                    session, team_id, now.astimezone(tz).date(), tz=tz
                )
                if can_manage
                else []
            ),
            upcoming_events=(
                await event_service.list_events(
                    session, actor, team_id=team_id, from_=now
                )
                if can_names and live
                else []
            ),
        )
    )


# --- one volunteer -------------------------------------------------------------


@dataclass(frozen=True)
class VolunteerProfile:
    """/volunteers/{id}. `can_view` is the contact-detail tier and gates the
    impact and service-hours sections with it; `workload` is present only
    when the reader may see it."""

    volunteer: Volunteer
    team_ids: set[int]
    can_view: bool
    can_edit: bool
    field_defs: list[CustomFieldDef]
    workload: tuple[Decimal, Band] | None
    assignments: list[tuple[Membership, Team]]
    impact: list[volunteer_service.ImpactRow]
    involvements: list[elections_service.ProposalInvolvement]
    hours: event_service.HoursSummary | None
    spells: list[volunteer_service.MembershipSpell]
    account: AppUser | None
    paths: dict[int, str]
    assignable: dict[int, str]  # teams the reader may add them to
    photo_at: datetime | None


async def volunteer_profile(
    session: AsyncSession,
    actor: Actor,
    volunteer_id: int,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> Result[VolunteerProfile, NotFound]:
    volunteer = await volunteer_service.get(session, volunteer_id)
    if volunteer is None:
        return not_found("volunteer", volunteer_id)
    team_ids = await volunteer_team_ids(session, volunteer_id)
    can_view = actor.can_view_volunteer(volunteer_id, team_ids)
    impact: list[volunteer_service.ImpactRow] = []
    hours = None
    if can_view:
        holes = await volunteer_service.impact(session, actor, volunteer_id)
        impact = holes.value if isinstance(holes, Ok) else []
        served = await event_service.hours_for_volunteer(
            session, actor, volunteer_id, now=now
        )
        hours = served.value if isinstance(served, Ok) else None
    tree = await team_service.tree(session)
    workload = await workload_service.visible_scores(
        session, actor, {volunteer_id: team_ids}
    )
    return Ok(
        VolunteerProfile(
            volunteer=volunteer,
            team_ids=team_ids,
            can_view=can_view,
            can_edit=actor.can_edit_volunteer(volunteer_id, team_ids),
            field_defs=await custom_field_service.list_defs(session),
            workload=workload.get(volunteer_id),
            assignments=await volunteer_service.assignments(session, volunteer_id),
            impact=impact,
            # scoped inside the service: only proposals this actor may see
            involvements=await elections_service.involving(
                session, actor, volunteer_id, today=now.astimezone(tz).date()
            ),
            hours=hours,
            spells=await volunteer_service.timeline(session, volunteer_id, tz=tz),
            account=await user_service.account_for_volunteer(session, volunteer_id),
            paths=tree.paths,
            assignable={
                t.id: tree.paths[t.id]
                for t in tree.teams
                if actor.can_manage_team(t.id)
            },
            photo_at=(await photo_service.versions(session, [volunteer_id])).get(
                volunteer_id
            ),
        )
    )


# --- one proposal --------------------------------------------------------------


@dataclass(frozen=True)
class ProposalWorkroom:
    """/elections/{id}. A manager runs the seat; a voter is somebody on the
    roll, and `my_scores` is their own ballot (empty until cast)."""

    view: elections_service.ProposalDetail
    can_manage: bool
    is_voter: bool
    my_scores: dict[int, int]  # candidate id -> 0-5
    workload: dict[int, tuple[Decimal, Band]]  # by candidate's volunteer id
    volunteer_options: dict[int, str]  # for the nominate and add-voter pickers

    @property
    def proposal(self) -> Proposal:
        return self.view.proposal

    @property
    def phase(self) -> elections_service.ProposalPhase | None:
        return self.view.phase

    @property
    def names(self) -> dict[int, str]:
        """candidate id -> the candidate's name."""
        return {cv.candidate.id: cv.volunteer.full_name for cv in self.view.candidates}


async def proposal_workroom(
    session: AsyncSession,
    actor: Actor,
    proposal_id: int,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> Result[ProposalWorkroom, DomainError]:
    shown = await elections_service.detail(
        session, actor, proposal_id, today=now.astimezone(tz).date()
    )
    if isinstance(shown, Err):
        return shown
    view = shown.value
    can_manage = actor.can_manage_team(view.proposal.team_id)
    is_voter = actor.volunteer_id is not None and (
        proposal_id in actor.voter_proposal_ids
    )
    my_scores: dict[int, int] = {}
    if is_voter:
        mine = await elections_service.my_scores(
            session, actor, proposal_id, actor.volunteer_id
        )
        if isinstance(mine, Err):
            return mine
        my_scores = mine.value
    team_sets = await team_ids_map(session, [cv.volunteer.id for cv in view.candidates])
    return Ok(
        ProposalWorkroom(
            view=view,
            can_manage=can_manage,
            is_voter=is_voter,
            my_scores=my_scores,
            workload=await workload_service.visible_scores(session, actor, team_sets),
            volunteer_options=(
                await volunteer_service.name_map(session)
                if can_manage or is_voter
                else {}
            ),
        )
    )
