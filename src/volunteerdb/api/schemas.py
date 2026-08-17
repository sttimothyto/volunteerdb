from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..models import ROLE_LABELS, FieldType, TeamRole


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


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


class PhotoMetaOut(BaseModel):
    volunteer_id: int
    content_type: str
    size_bytes: int  # always <= services.photos.PHOTO_MAX_BYTES
    uploaded_at: datetime


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
    workload_weight: float | None = None  # null = unweighted
    home_doc_url: str | None = None  # public Google Doc behind /ministries/


class HomeDocPatch(BaseModel):
    """Body of PATCH /teams/{id}/home-doc — deliberately separate from the
    admin-only TeamPatch: leaders/seconds/core members may set only this."""

    url: str | None = Field(default=None, max_length=500)


class TeamWithPath(TeamOut):
    path: str


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_team_id: int | None = None
    description: str | None = None
    workload_weight: float | None = Field(default=None, ge=0)


class TeamPatch(BaseModel):
    name: str | None = None
    parent_team_id: int | None = None
    clear_parent: bool = False  # set true to move a sub-team to top level
    description: str | None = None
    is_active: bool | None = None
    workload_weight: float | None = Field(default=None, ge=0)
    clear_workload_weight: bool = False  # set true to unweight the team


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


# --- workload ---


class BandOut(BaseModel):
    label: str
    color: str
    upper: float | None  # inclusive threshold; null = unbounded (last band)


class WorkloadConfigOut(BaseModel):
    multipliers: dict[TeamRole, float]
    bands: list[BandOut]


class WorkloadConfigIn(WorkloadConfigOut):
    pass


class WorkloadScoreOut(BaseModel):
    volunteer_id: int
    score: float
    band: str
    color: str


# --- reports ---


class AssignmentOut(BaseModel):
    membership_id: int
    team: TeamOut
    role: TeamRole
    role_label: str


class ImpactOut(BaseModel):
    team: TeamOut
    role: TeamRole
    role_label: str
    leaders_left: int
    leadership_left: int


class TimelineSegmentOut(BaseModel):
    role: TeamRole
    role_label: str
    start: datetime
    end: datetime | None  # null = ongoing


class TimelineSpellOut(BaseModel):
    team_id: int
    team_name: str
    team_deleted: bool
    role: TeamRole
    role_label: str
    start: date
    end: date | None  # null = ongoing
    segments: list[TimelineSegmentOut]


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


class CandidateOut(ORMModel):
    id: int
    volunteer_id: int
    volunteer_name: str = ""
    note: str | None
    nominated_by: int | None
    created_at: datetime
    # the candidate's current commitments — the overwork check
    assignments: list[AssignmentOut] = []


class VoterIn(BaseModel):
    volunteer_id: int


class VoterOut(ORMModel):
    id: int
    volunteer_id: int
    volunteer_name: str = ""
    has_account: bool = False  # without an active account they cannot vote
    has_voted: bool = False  # turnout flag; scores are never exposed


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


class ProposalDetailOut(BaseModel):
    proposal: ProposalOut
    path: str
    candidates: list[CandidateOut]
    voters: list[VoterOut]
    tally: TallyOut | None  # null until voting has concluded


class InvolvementOut(BaseModel):
    """One proposal touching a volunteer (GET /volunteers/{id}/proposals)."""

    proposal: ProposalOut
    path: str
    as_candidate: bool
    as_voter: bool
    appointed: bool  # this volunteer is the appointed candidate


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


class EventSlotPatch(BaseModel):
    # None = leave unchanged (capacity cannot be cleared to unlimited by PATCH)
    name: str | None = None
    capacity: int | None = Field(default=None, ge=1)
    position: int | None = None


class EventSlotOut(ORMModel):
    id: int
    name: str
    capacity: int | None  # null = unlimited
    position: int


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


class EventAssignmentOut(ORMModel):
    id: int
    slot_id: int
    event_id: int
    volunteer_id: int
    volunteer_name: str = ""
    kind: str  # signup/assigned/sub — provenance only
    sub_requested: bool = False  # an open substitution call exists


class SlotViewOut(BaseModel):
    slot: EventSlotOut
    entries: list[EventAssignmentOut]
    open_spots: int | None  # null = unlimited


class EventRsvpIn(BaseModel):
    available: bool
    note: str | None = Field(default=None, max_length=200)


class EventRsvpOut(ORMModel):
    volunteer_id: int
    volunteer_name: str = ""
    available: bool
    note: str | None


class AttendanceRowOut(BaseModel):
    """Derived attendance for one assignment of a past event."""

    assignment_id: int
    volunteer_id: int
    volunteer_name: str
    slot_name: str
    attended: bool
    hours: float
    overridden: bool  # a manager recorded an exception


class EventDetailOut(BaseModel):
    event: EventOut
    path: str
    slots: list[SlotViewOut]
    rsvps: list[EventRsvpOut]
    # only for managers of the team, and only once the event has ended
    attendance: list[AttendanceRowOut] | None = None


class EventAssignIn(BaseModel):
    volunteer_id: int | None = None  # omitted: sign yourself up
    # self sign-ups only: copy the sign-up onto later weeks of the series
    repeat_series: bool = False


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
    last_login_at: datetime | None


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


def role_label(role: TeamRole) -> str:
    return ROLE_LABELS[role]
