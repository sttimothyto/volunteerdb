"""The JSON API's wire shapes, and how each one is built from what a service
returns.

An ``Out`` model is a shape; its ``of(...)`` classmethod is the one place that
shape is assembled -- the redaction a caller's rights impose, the label a role
carries, the flag a row does not hold itself. Routers call these and never
adjust a model after the fact, so a change to what the API says about a
thing is a change in one method here, next to the fields it fills.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from ..models import (
    ROLE_LABELS,
    AppUser,
    Event,
    EventAssignment,
    EventRsvp,
    EventSlot,
    FieldType,
    Membership,
    Proposal,
    ProposalCandidate,
    ProposalVoter,
    Team,
    TeamRole,
    Volunteer,
    VolunteerPhoto,
)
from ..permissions import Actor
from ..services import elections as election_service
from ..services import events as event_service
from ..services import task_force as task_force_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from ..services.reports import CoverageRow
from ..sheets.common import sheet_url
from ..star import StarResult


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def role_label(role: TeamRole) -> str:
    return ROLE_LABELS[role]


# --- volunteers ---


class VolunteerOut(ORMModel):
    id: int
    first_name: str
    last_name: str
    # contact fields are nulled when the caller lacks detail rights on this person
    email: str | None = None
    phone: str | None = None
    notes: str | None = None
    is_active: bool
    # admin-defined custom field values; nulled like contact details
    custom: dict[str, Any] | None = None
    # tri-state: computed on the list/detail endpoints, null where not (embeds)
    has_photo: bool | None = None

    @classmethod
    def redacted(
        cls,
        actor: Actor,
        volunteer: Volunteer,
        team_ids: set[int],
        *,
        has_photo: bool | None = None,
    ) -> VolunteerOut:
        """The volunteer as `actor` may see them. Everyone may see names;
        contact details, notes and custom values need closer ties (the
        permission model's can_view_volunteer and can_edit_volunteer, over
        the volunteer's `team_ids`)."""
        out = cls.model_validate(volunteer)
        out.has_photo = has_photo
        if not actor.can_view_volunteer(volunteer.id, team_ids):
            out.email = out.phone = out.notes = out.custom = None
        elif not actor.can_edit_volunteer(volunteer.id, team_ids):
            out.notes = None
        return out


class PhotoMetaOut(BaseModel):
    volunteer_id: int
    content_type: str
    size_bytes: int  # always <= services.photos.PHOTO_MAX_BYTES
    uploaded_at: datetime

    @classmethod
    def of(cls, record: VolunteerPhoto) -> PhotoMetaOut:
        return cls(
            volunteer_id=record.volunteer_id,
            content_type=record.content_type,
            size_bytes=len(record.image),
            uploaded_at=record.uploaded_at,
        )


class VolunteerIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str | None = None
    phone: str | None = None
    notes: str | None = None


class VolunteerPatch(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    notes: str | None = None
    is_active: bool | None = None
    # partial merge of custom field values; a null value clears that key
    custom: dict[str, Any] | None = None


# --- custom fields ---


class CustomFieldDefOut(ORMModel):
    id: int
    key: str
    label: str
    field_type: str
    options: list[str] | None
    show_in_list: bool
    position: int
    is_active: bool


class CustomFieldDefIn(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    field_type: FieldType
    options: list[str] | None = None
    show_in_list: bool = False
    position: int = 0


class CustomFieldDefPatch(BaseModel):
    label: str | None = None
    options: list[str] | None = None
    show_in_list: bool | None = None
    position: int | None = None
    is_active: bool | None = None


# --- teams ---


class TeamOut(ORMModel):
    id: int
    name: str
    parent_team_id: int | None
    description: str | None
    is_active: bool
    # 0 = excluded from workload scores; there is no "unset" any more, because
    # NULL and 0 always scored the same. An as-of read can still surface a
    # team_history row whose weight predates the NOT NULL default and is NULL;
    # it means 0, so fold it here rather than 500 the whole response.
    workload_weight: float = 0
    home_doc_url: str | None = None  # public Google Doc behind /ministries/

    @field_validator("workload_weight", mode="before")
    @classmethod
    def _weight_or_zero(cls, value: object) -> object:
        return 0 if value is None else value


class HomeDocPatch(BaseModel):
    """Body of PATCH /teams/{id}/home-doc — deliberately separate from the
    admin-only TeamPatch: leaders/seconds/core members may set only this."""

    url: str | None = Field(default=None, max_length=500)


class TeamSheetOut(ORMModel):
    """A team's roster spreadsheet and the last sync's outcome
    (jobs.roster_sync)."""

    file_id: str | None = None
    file_name: str | None = None
    url: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_synced_at: datetime | None = None

    @model_validator(mode="after")
    def _links(self) -> TeamSheetOut:
        """The link is derived, never stored: the file id is what survives a
        team rename, and Google rewriting its own URL shapes."""
        if self.file_id:
            self.url = sheet_url(self.file_id)
        return self


class RosterSheetPatch(BaseModel):
    """Body of PATCH /teams/{id}/roster-sheet — leaders and seconds, but not
    core members as HomeDocPatch allows: a roster sheet carries members'
    addresses and phone numbers, and linking one hands it a bulk write over
    the roster."""

    url: str = Field(max_length=500)


class RosterSheetSync(BaseModel):
    """Body of POST /teams/{id}/roster-sheet/sync."""

    # "import" applies the sheet's rows then writes the result back; "export"
    # skips the read and overwrites the sheet from the database. Importing
    # never removes anybody -- services.roster_sheets explains why.
    direction: Literal["import", "export"] = "import"


class TeamWithPath(TeamOut):
    path: str

    @classmethod
    def of(cls, team: Team, path: str) -> TeamWithPath:
        return cls(**TeamOut.model_validate(team).model_dump(), path=path)


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_team_id: int | None = None
    description: str | None = None
    # A new ministry is ordinary work, not zero work: omitting the field starts
    # the team at 1, the same default the GUI's "New team" dialog offers. An
    # explicit null still means 0, which is how a team is excluded from
    # workload scores (services.teams.create normalises it). Creation only —
    # TeamPatch keeps None meaning "leave alone".
    workload_weight: float | None = Field(default=1, ge=0)


class TeamPatch(BaseModel):
    name: str | None = None
    parent_team_id: int | None = None
    clear_parent: bool = False  # set true to move a sub-team to top level
    description: str | None = None
    is_active: bool | None = None
    workload_weight: float | None = Field(default=None, ge=0)
    # set true to put the weight back to 0 (which is what "unweighted" means)
    clear_workload_weight: bool = False


# --- memberships ---


class MembershipOut(ORMModel):
    id: int
    volunteer_id: int
    team_id: int
    role: TeamRole


class MembershipIn(BaseModel):
    volunteer_id: int
    team_id: int
    role: TeamRole


class RosterEntry(BaseModel):
    membership_id: int
    volunteer: VolunteerOut
    role: TeamRole
    role_label: str

    @classmethod
    def of(
        cls, membership: Membership, volunteer: Volunteer, *, actor: Actor, team_id: int
    ) -> RosterEntry:
        """One line of a team's roster as `actor` may read it. Full-roster
        rights on the team show the contact details; notes need management
        rights on it; anybody else gets the redacted view."""
        if actor.can_view_full_roster(team_id):
            out = VolunteerOut.model_validate(volunteer)
            if not actor.can_manage_team(team_id):
                out.notes = None
        else:
            out = VolunteerOut.redacted(actor, volunteer, {team_id})
        return cls(
            membership_id=membership.id,
            volunteer=out,
            role=membership.role,
            role_label=role_label(membership.role),
        )


# --- workload ---


class BandOut(BaseModel):
    label: str
    color: str
    upper: float | None  # inclusive threshold; null = unbounded (last band)

    @classmethod
    def of(cls, band: workload_service.Band) -> BandOut:
        return cls(
            label=band.label,
            color=band.color,
            upper=None if band.upper is None else float(band.upper),
        )


class WorkloadConfigOut(BaseModel):
    multipliers: dict[TeamRole, float]
    bands: list[BandOut]

    @classmethod
    def of(cls, config: workload_service.WorkloadConfig) -> WorkloadConfigOut:
        return cls(
            multipliers={role: float(m) for role, m in config.multipliers.items()},
            bands=[BandOut.of(b) for b in config.bands],
        )


class WorkloadConfigIn(WorkloadConfigOut):
    def to_config(self) -> workload_service.WorkloadConfig:
        """The service's value: Decimals, because asyncpg's numeric codec and
        the arithmetic both want them exact."""
        return workload_service.WorkloadConfig(
            multipliers={role: Decimal(str(m)) for role, m in self.multipliers.items()},
            bands=[
                workload_service.Band(
                    b.label, b.color, None if b.upper is None else Decimal(str(b.upper))
                )
                for b in self.bands
            ],
        )


class WorkloadScoreOut(BaseModel):
    volunteer_id: int
    score: float
    band: str
    color: str

    @classmethod
    def of(
        cls, volunteer_id: int, score: Decimal, band: workload_service.Band
    ) -> WorkloadScoreOut:
        return cls(
            volunteer_id=volunteer_id,
            score=float(score),
            band=band.label,
            color=band.color,
        )


# --- reports ---


class AssignmentOut(BaseModel):
    membership_id: int
    team: TeamOut
    role: TeamRole
    role_label: str

    @classmethod
    def of(cls, membership: Membership, team: Team) -> AssignmentOut:
        return cls(
            membership_id=membership.id,
            team=TeamOut.model_validate(team),
            role=membership.role,
            role_label=role_label(membership.role),
        )


class ImpactOut(BaseModel):
    team: TeamOut
    role: TeamRole
    role_label: str
    leaders_left: int
    leadership_left: int

    @classmethod
    def of(cls, row: volunteer_service.ImpactRow) -> ImpactOut:
        return cls(
            team=TeamOut.model_validate(row.team),
            role=row.role,
            role_label=role_label(row.role),
            leaders_left=row.leaders_left,
            leadership_left=row.leadership_left,
        )


class TimelineSegmentOut(BaseModel):
    role: TeamRole
    role_label: str
    start: datetime
    end: datetime | None  # null = ongoing

    @classmethod
    def of(cls, segment: volunteer_service.RoleSegment) -> TimelineSegmentOut:
        return cls(
            role=segment.role,
            role_label=role_label(segment.role),
            start=segment.start,
            end=segment.end,
        )


class TimelineSpellOut(BaseModel):
    team_id: int
    team_name: str
    team_deleted: bool
    role: TeamRole
    role_label: str
    start: date
    end: date | None  # null = ongoing
    segments: list[TimelineSegmentOut]

    @classmethod
    def of(cls, spell: volunteer_service.MembershipSpell) -> TimelineSpellOut:
        return cls(
            team_id=spell.team_id,
            team_name=spell.team_name,
            team_deleted=spell.team_deleted,
            role=spell.role,
            role_label=role_label(spell.role),
            start=spell.start,
            end=spell.end,
            segments=[TimelineSegmentOut.of(seg) for seg in spell.segments],
        )


class CoverageOut(BaseModel):
    team_id: int
    path: str
    leader: int
    second: int
    core: int
    member: int
    total: int
    missing_leader: bool
    missing_second: bool

    @classmethod
    def of(cls, row: CoverageRow) -> CoverageOut:
        return cls(
            team_id=row.team.id,
            path=row.path,
            leader=row.counts.get(TeamRole.leader, 0),
            second=row.counts.get(TeamRole.second, 0),
            core=row.counts.get(TeamRole.core, 0),
            member=row.counts.get(TeamRole.member, 0),
            total=row.total,
            missing_leader=row.missing_leader,
            missing_second=row.missing_second,
        )


# --- dashboard statistics ---
#
# Mirrors services.stats one for one. A null section is one this caller may
# not see (or, for the live-only ones, one a snapshot cannot answer) — as
# opposed to a zero, which is a real count. The service decides; these
# schemas only carry the answer.


class ParishStatsOut(ORMModel):
    active_volunteers: int
    inactive_volunteers: int
    active_teams: int
    assignments: int
    unassigned_volunteers: int
    accounts: int | None
    ministries_per_volunteer: float


class GapTeamOut(ORMModel):
    team_id: int
    path: str
    missing_leader: bool
    missing_second: bool


class BandCountOut(ORMModel):
    label: str
    color: str
    count: int


class PhaseCountOut(ORMModel):
    phase: str
    label: str
    count: int


class LeadershipStatsOut(ORMModel):
    teams: int
    people: int
    people_without_email: int
    teams_without_leader: int | None
    teams_without_second: int | None
    gap_teams: list[GapTeamOut]
    bands: list[BandCountOut] | None
    understaffed_events: int | None
    open_elections: list[PhaseCountOut] | None


class PersonalStatsOut(ORMModel):
    upcoming_duties: int
    next_duty_at: datetime | None
    next_duty_title: str | None
    next_duty_slot: str | None
    claimable_subs: int
    ballots_waiting: int
    hours_served: Decimal
    events_attended: int


class DashboardStatsOut(ORMModel):
    parish: ParishStatsOut | None
    leadership: LeadershipStatsOut | None
    personal: PersonalStatsOut | None
    live: bool


# --- elections ---


class CandidateIn(BaseModel):
    volunteer_id: int
    note: str | None = None  # the nominator's "why them" reasoning


class ProposalCreateIn(BaseModel):
    team_id: int
    role: TeamRole
    nomination_deadline: date  # last day to nominate, inclusive
    voting_deadline: date  # last day to vote, inclusive
    notes: str | None = None
    candidates: list[CandidateIn] = Field(min_length=1)


class ProposalPatch(BaseModel):
    # None = leave unchanged
    nomination_deadline: date | None = None
    voting_deadline: date | None = None
    notes: str | None = None


class ProposalOut(ORMModel):
    id: int
    team_id: int
    role: TeamRole
    role_label: str = ""
    status: str
    phase: str | None = None  # nominating/voting/concluded; null once decided
    notes: str | None
    nomination_deadline: date
    voting_deadline: date
    appointed_candidate_id: int | None
    created_by: int | None
    created_at: datetime
    decided_by: int | None
    decided_at: datetime | None

    @classmethod
    def of(cls, proposal: Proposal, *, today: date) -> ProposalOut:
        """The row plus what derives from the parish day: the phase an open
        proposal is in (elections.phase_of)."""
        out = cls.model_validate(proposal)
        out.role_label = role_label(TeamRole(proposal.role))
        phase = election_service.phase_of(proposal, today)
        out.phase = phase.value if phase else None
        return out


class CandidateOut(ORMModel):
    id: int
    volunteer_id: int
    volunteer_name: str = ""
    note: str | None
    nominated_by: int | None
    created_at: datetime
    # the candidate's current commitments — the overwork check
    assignments: list[AssignmentOut] = []

    @classmethod
    def of(
        cls,
        candidate: ProposalCandidate,
        volunteer: Volunteer | None,
        assignments: list[tuple[Membership, Team]] = (),
    ) -> CandidateOut:
        out = cls.model_validate(candidate)
        out.volunteer_name = volunteer.full_name if volunteer else ""
        out.assignments = [AssignmentOut.of(m, t) for m, t in assignments]
        return out


class VoterIn(BaseModel):
    volunteer_id: int


class VoterOut(ORMModel):
    id: int
    volunteer_id: int
    volunteer_name: str = ""
    has_account: bool = False  # without an active account they cannot vote
    has_voted: bool = False  # turnout flag; scores are never exposed

    @classmethod
    def of(
        cls,
        voter: ProposalVoter,
        volunteer: Volunteer | None,
        *,
        has_account: bool = False,
        has_voted: bool = False,
    ) -> VoterOut:
        out = cls.model_validate(voter)
        out.volunteer_name = volunteer.full_name if volunteer else ""
        out.has_account = has_account
        out.has_voted = has_voted
        return out


class BallotIn(BaseModel):
    scores: dict[int, int]  # candidate id -> 0-5; omitted candidates score 0


class CandidateTallyOut(BaseModel):
    candidate_id: int
    volunteer_name: str
    total: int  # scoring-round sum


class TallyOut(BaseModel):
    ballot_count: int
    totals: list[CandidateTallyOut]  # sorted by total, best first
    finalist_ids: list[int] | None
    runoff: dict[int, int] | None  # finalist id -> ballots preferring them
    no_preference: int | None
    winner_candidate_id: int | None  # null: no candidates, or a reported tie
    tie: bool
    tied_candidate_ids: list[int]

    @classmethod
    def of(cls, result: StarResult, names: dict[int, str]) -> TallyOut:
        """The STAR result with the candidates named (`names`: candidate id
        to the volunteer's name), totals best first."""
        ranked = sorted(result.totals.items(), key=lambda kv: (-kv[1], kv[0]))
        return cls(
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


class ProposalDetailOut(BaseModel):
    proposal: ProposalOut
    path: str
    candidates: list[CandidateOut]
    voters: list[VoterOut]
    tally: TallyOut | None  # null until voting has concluded

    @classmethod
    def of(
        cls, view: election_service.ProposalDetail, *, today: date
    ) -> ProposalDetailOut:
        names = {c.candidate.id: c.volunteer.full_name for c in view.candidates}
        return cls(
            proposal=ProposalOut.of(view.proposal, today=today),
            path=view.path,
            candidates=[
                CandidateOut.of(c.candidate, c.volunteer, c.assignments)
                for c in view.candidates
            ],
            voters=[
                VoterOut.of(
                    v.voter,
                    v.volunteer,
                    has_account=v.has_account,
                    has_voted=v.has_voted,
                )
                for v in view.voters
            ],
            tally=TallyOut.of(view.tally, names) if view.tally else None,
        )


class InvolvementOut(BaseModel):
    """One proposal touching a volunteer (GET /volunteers/{id}/proposals)."""

    proposal: ProposalOut
    path: str
    as_candidate: bool
    as_voter: bool
    appointed: bool  # this volunteer is the appointed candidate

    @classmethod
    def of(
        cls, involvement: election_service.ProposalInvolvement, *, today: date
    ) -> InvolvementOut:
        return cls(
            proposal=ProposalOut.of(involvement.proposal, today=today),
            path=involvement.path,
            as_candidate=involvement.as_candidate,
            as_voter=involvement.as_voter,
            appointed=involvement.appointed,
        )


class AppointIn(BaseModel):
    candidate_id: int


class NewRoundIn(BaseModel):
    nomination_deadline: date
    voting_deadline: date


# --- events ---
# ("Event" prefixes throughout: AssignmentOut above already means a
# volunteer's team-role assignment, not an event slot assignment)


class EventSlotIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    capacity: int | None = Field(default=None, ge=1)  # null = unlimited
    position: int = 0
    description: str | None = Field(default=None, max_length=300)


class EventSlotPatch(BaseModel):
    # None = leave unchanged (capacity cannot be cleared to unlimited by PATCH)
    name: str | None = None
    capacity: int | None = Field(default=None, ge=1)
    position: int | None = None
    # sent explicitly as null this one does clear: a note is decoration, and
    # a leader who wants it gone has no other way to say so
    description: str | None = Field(default=None, max_length=300)


class EventSlotOut(ORMModel):
    id: int
    name: str
    capacity: int | None  # null = unlimited
    position: int
    description: str | None


class EventCreateIn(BaseModel):
    team_id: int
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    location: str | None = None
    starts_at: datetime  # timezone-aware instants
    ends_at: datetime
    slots: list[EventSlotIn] = []  # empty: one unlimited "Volunteers" slot
    repeat_weekly_until: date | None = None  # inclusive, parish wall clock


class EventPatch(BaseModel):
    # None = leave unchanged
    title: str | None = None
    description: str | None = None
    location: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class EventOut(ORMModel):
    id: int
    team_id: int
    title: str
    description: str | None
    location: str | None
    starts_at: datetime
    ends_at: datetime
    status: str
    cancelled_at: datetime | None
    created_at: datetime
    series_id: UUID | None  # shared by weekly repeats; null = standalone


class EventSummaryOut(BaseModel):
    """One row of GET /events: the event plus the caller's own standing."""

    event: EventOut
    path: str
    filled: int
    capacity: int | None  # null = at least one unlimited slot
    my_assignment_id: int | None
    my_rsvp_available: bool | None  # null = not answered

    @classmethod
    def of(cls, summary: event_service.EventSummary) -> EventSummaryOut:
        return cls(
            event=EventOut.model_validate(summary.event),
            path=summary.path,
            filled=summary.filled,
            capacity=summary.capacity,
            my_assignment_id=(
                summary.my_assignment.id if summary.my_assignment else None
            ),
            my_rsvp_available=summary.my_rsvp.available if summary.my_rsvp else None,
        )


class EventAssignmentOut(ORMModel):
    id: int
    slot_id: int
    event_id: int
    volunteer_id: int
    volunteer_name: str = ""
    kind: str  # signup/assigned/sub — provenance only
    sub_requested: bool = False  # an open substitution call exists
    notify_7d: bool = False  # reminder-stage preferences (see EventAssignIn)
    notify_24h: bool = True

    @classmethod
    def of(
        cls,
        assignment: EventAssignment,
        volunteer: Volunteer,
        *,
        sub_requested: bool = False,
    ) -> EventAssignmentOut:
        out = cls.model_validate(assignment)
        out.volunteer_name = volunteer.full_name
        out.sub_requested = sub_requested
        return out


class SlotViewOut(BaseModel):
    slot: EventSlotOut
    entries: list[EventAssignmentOut]
    open_spots: int | None  # null = unlimited

    @classmethod
    def of(cls, view: event_service.SlotView, sub_wanted: set[int]) -> SlotViewOut:
        """`sub_wanted`: the assignment ids with an open substitution call."""
        return cls(
            slot=EventSlotOut.model_validate(view.slot),
            entries=[
                EventAssignmentOut.of(a, v, sub_requested=a.id in sub_wanted)
                for a, v in view.entries
            ],
            open_spots=view.open_spots,
        )


class EventRsvpIn(BaseModel):
    available: bool
    note: str | None = Field(default=None, max_length=200)


class EventRsvpOut(ORMModel):
    volunteer_id: int
    volunteer_name: str = ""
    available: bool
    note: str | None

    @classmethod
    def of(cls, rsvp: EventRsvp, volunteer: Volunteer) -> EventRsvpOut:
        out = cls.model_validate(rsvp)
        out.volunteer_name = volunteer.full_name
        return out


class AttendanceRowOut(BaseModel):
    """Derived attendance for one assignment of a past event."""

    assignment_id: int
    volunteer_id: int
    volunteer_name: str
    slot_name: str
    attended: bool
    hours: float
    overridden: bool  # a manager recorded an exception

    @classmethod
    def of(
        cls,
        assignment: EventAssignment,
        slot: EventSlot,
        volunteer: Volunteer,
        event: Event,
    ) -> AttendanceRowOut:
        """Attendance is derived (events.effective): the scheduled duration
        unless a manager recorded an exception, which `overridden` reports."""
        attended, hours = event_service.effective(assignment, event)
        return cls(
            assignment_id=assignment.id,
            volunteer_id=assignment.volunteer_id,
            volunteer_name=volunteer.full_name,
            slot_name=slot.name,
            attended=attended,
            hours=float(hours),
            overridden=assignment.attended_override is not None
            or assignment.hours_override is not None,
        )


class EventDetailOut(BaseModel):
    event: EventOut
    path: str
    slots: list[SlotViewOut]
    rsvps: list[EventRsvpOut]
    # only for managers of the team, and only once the event has ended
    attendance: list[AttendanceRowOut] | None = None

    @classmethod
    def of(
        cls,
        view: event_service.EventDetail,
        *,
        attendance: list[AttendanceRowOut] | None = None,
    ) -> EventDetailOut:
        sub_wanted = {a.id for _, a in view.open_subs}
        return cls(
            event=EventOut.model_validate(view.event),
            path=view.path,
            slots=[SlotViewOut.of(sv, sub_wanted) for sv in view.slots],
            rsvps=[EventRsvpOut.of(r, v) for r, v in view.rsvps],
            attendance=attendance,
        )


class EventAssignIn(BaseModel):
    volunteer_id: int | None = None  # omitted: sign yourself up
    # self sign-ups only: copy the sign-up onto later weeks of the series
    repeat_series: bool = False
    # Reminder-stage preferences for self sign-ups (manager assignments keep
    # the defaults — the volunteer never chose). The 7-day stage is off by
    # default: it restates the "you have been scheduled" notice, and on a
    # 200-message/day mail allowance that made it a third of all event mail
    # for nothing. Opt in per assignment; the stage itself is unchanged.
    notify_7d: bool = False
    notify_24h: bool = True


class SubRequestIn(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class SubRequestOut(ORMModel):
    id: int
    assignment_id: int
    note: str | None
    status: str
    claimed_by_volunteer_id: int | None
    created_at: datetime
    resolved_at: datetime | None


class AttendanceIn(BaseModel):
    attended: bool | None  # null clears the override back to auto
    hours: float | None = Field(default=None, ge=0)


class VolunteerHoursOut(BaseModel):
    """Derived service record over past, non-cancelled events."""

    volunteer_id: int
    total_hours: float
    events_attended: int

    @classmethod
    def of(
        cls, volunteer_id: int, summary: event_service.HoursSummary
    ) -> VolunteerHoursOut:
        return cls(
            volunteer_id=volunteer_id,
            total_hours=float(summary.total_hours),
            events_attended=summary.events_attended,
        )


# --- users ---


class UserOut(ORMModel):
    id: int
    email: str
    volunteer_id: int | None
    is_admin: bool
    is_active: bool
    has_password: bool = False
    invite_token: str | None = None
    invite_expires_at: datetime | None = None  # when that link stops working
    # An address waiting on its own confirmation link. Reported like the
    # invite pair above — the value with the window it is good for, so a
    # caller can tell a live change from a stale one. The token that would
    # redeem it is never exposed.
    pending_email: str | None = None
    email_change_expires_at: datetime | None = None
    last_login_at: datetime | None

    @classmethod
    def of(cls, user: AppUser, invite_token: str | None = None) -> UserOut:
        """An account as an admin sees it. `invite_token` is passed in, never
        read off the row: only the digest is stored (services.users
        ._issue_invite), so a freshly minted link is the only one that exists
        in readable form and the column itself must never be serialized.
        `invite_expires_at` still comes off the row — a caller may always
        learn that a link is outstanding, just not what it is."""
        out = cls.model_validate(user)
        out.has_password = user.password_hash is not None
        out.invite_token = invite_token
        return out

    @classmethod
    def own(cls, user: AppUser) -> UserOut:
        """The caller's own account (GET /auth/me, and what the sign-in
        flows answer): neither the invite link nor its window, since an
        account has no business seeing its own invite."""
        out = cls.of(user)
        out.invite_expires_at = None
        return out


class UserIn(BaseModel):
    email: EmailStr
    volunteer_id: int | None = None
    is_admin: bool = False
    # Held to passwords.check like every other way in — a weak one comes back
    # as a 422 naming the rule it broke. Omit it for the usual invite flow.
    password: str | None = None


class UserPatch(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None
    volunteer_id: int | None = None  # explicit null unlinks; omit to leave alone


# --- auth ---


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    token: str


# --- personal event views (GET /api/events/mine, /api/events/claimable) ------


class MyDutyOut(BaseModel):
    """One upcoming commitment. The GUI's "My duties" list, over JSON."""

    assignment_id: int
    event: EventOut
    slot_id: int
    slot_name: str
    open_sub_request_id: int | None  # a substitute call you already opened

    @classmethod
    def of(cls, duty: event_service.MyDuty) -> MyDutyOut:
        return cls(
            assignment_id=duty.assignment.id,
            event=EventOut.model_validate(duty.event),
            slot_id=duty.slot.id,
            slot_name=duty.slot.name,
            open_sub_request_id=duty.open_sub.id if duty.open_sub else None,
        )


class ClaimableSubOut(BaseModel):
    """A teammate's open substitution call that the caller could take over."""

    sub_request_id: int
    assignment_id: int
    event: EventOut
    slot_id: int
    slot_name: str
    asked_by_volunteer_id: int
    asked_by_name: str
    note: str | None
    path: str

    @classmethod
    def of(cls, claimable: event_service.ClaimableSub) -> ClaimableSubOut:
        return cls(
            sub_request_id=claimable.sub.id,
            assignment_id=claimable.assignment.id,
            event=EventOut.model_validate(claimable.event),
            slot_id=claimable.slot.id,
            slot_name=claimable.slot.name,
            asked_by_volunteer_id=claimable.volunteer.id,
            asked_by_name=claimable.volunteer.full_name,
            note=claimable.sub.note,
            path=claimable.path,
        )


class SimilarEventOut(BaseModel):
    """An advisory double-booking hit. `title` is null when the colliding event
    belongs to a team outside the caller's view: the when and where is the
    warning, the details stay that team's."""

    starts_at: datetime
    ends_at: datetime
    location: str
    team_path: str
    title: str | None

    @classmethod
    def of(cls, hit: event_service.SimilarEvent) -> SimilarEventOut:
        return cls(
            starts_at=hit.starts_at,
            ends_at=hit.ends_at,
            location=hit.location,
            team_path=hit.team_path,
            title=hit.title,
        )


class BallotOut(BaseModel):
    """The caller's own scores on one proposal, candidate id -> 0..5. Empty
    until they vote. Nobody else's ballot is readable anywhere."""

    scores: dict[int, int]


# --- account self-service (api/auth.py) --------------------------------------


class PasswordIn(BaseModel):
    """Set or change the caller's own password.

    `current_password` is always required: an API token is only ever issued
    against a password (POST /auth/login), so a caller holding one can always
    produce it — unlike the GUI, where a session established by emailed code
    may set a first password without one.
    """

    current_password: str
    new_password: str = Field(min_length=1)


class EmailChangeIn(BaseModel):
    new_email: EmailStr


class EmailChangeConfirmIn(BaseModel):
    """The token from the link mailed to the new address."""

    token: str = Field(min_length=1)


class PendingEmailOut(BaseModel):
    pending_email: str | None
    email_change_expires_at: datetime | None

    @classmethod
    def of(cls, user: AppUser) -> PendingEmailOut:
        return cls(
            pending_email=user.pending_email,
            email_change_expires_at=user.email_change_expires_at,
        )


class RedeemInviteIn(BaseModel):
    """Spend an invite link. `password` is optional: without one the account
    stays email-code-only, exactly as on the /invite page."""

    token: str = Field(min_length=1)
    password: str | None = None
    agreed_to_confidentiality: bool = False


# --- task forces (api/events.py) ---------------------------------------------


class TaskForceOut(BaseModel):
    """The meta team an event was repointed to so several teams can staff it,
    and the teams whose rosters were copied into it (the owner is always one).

    A task force confers rights over the EVENT, never over the people it
    borrowed — see permissions.Actor and the permission matrix.
    """

    event_id: int
    team_id: int  # the meta team; membership of it gates sign-up
    # null only if the owner team was deleted while the task force was live: the
    # event then stays on the meta team at teardown rather than losing its
    # attendance record (services/task_force.teardown)
    owner_team_id: int | None
    sources: list[TeamWithPath]

    @classmethod
    def of(
        cls, view: task_force_service.TaskForceView, paths: dict[int, str]
    ) -> TaskForceOut:
        """`paths`: the team tree's display paths, for the source teams."""
        return cls(
            event_id=view.event.id,
            team_id=view.team_id,
            owner_team_id=view.owner_team_id,
            sources=[TeamWithPath.of(t, paths[t.id]) for t in view.sources],
        )


class CollaboratorIn(BaseModel):
    team_id: int


class SubstituteIn(BaseModel):
    """Hand a slot to a chosen teammate, rather than opening it to the team."""

    volunteer_id: int


# --- team home pages (api/teams.py) ------------------------------------------


class TeamPageOut(ORMModel):
    """The cached, sanitized state of a team's public page. `html` is omitted
    here: it is served to the world at /ministries/<slug>.html, and what an API
    caller needs is whether the last fetch worked."""

    team_id: int
    status: str
    fetched_at: datetime | None
    error: str | None
