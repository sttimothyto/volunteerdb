from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from ..models import ROLE_LABELS, FieldType, TeamRole
from ..sheets.common import sheet_url


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
    """A team's Google Drive roster sheet, and any repoint waiting on the
    nightly sync to check it (jobs.drive_sync)."""

    file_id: str | None = None
    file_name: str | None = None
    url: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_synced_at: datetime | None = None
    requested_file_id: str | None = None
    requested_url: str | None = None
    requested_import: bool = False

    @model_validator(mode="after")
    def _links(self) -> "TeamSheetOut":
        """Both links are derived, never stored: the Drive file id is what
        survives the rename a team rename triggers."""
        if self.file_id:
            self.url = sheet_url(self.file_id)
        if self.requested_file_id:
            self.requested_url = sheet_url(self.requested_file_id)
        return self


class RosterSheetPatch(BaseModel):
    """Body of PATCH /teams/{id}/roster-sheet. Admin-only, unlike HomeDocPatch:
    a roster sheet carries members' addresses and phone numbers, and adopting
    one hands it a bulk write over the roster."""

    url: str | None = Field(default=None, max_length=500)  # null withdraws
    # False regenerates the newly linked sheet from the database; True imports
    # its rows instead, through the importer's usual removal thresholds
    import_rows: bool = False


class TeamWithPath(TeamOut):
    path: str


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
    notify_7d: bool = True  # reminder-stage preferences (see EventAssignIn)
    notify_24h: bool = True


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
    # reminder-stage preferences for self sign-ups (manager assignments
    # keep the defaults — the volunteer never chose)
    notify_7d: bool = True
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


# --- personal event views (GET /api/events/mine, /api/events/claimable) ------


class MyDutyOut(BaseModel):
    """One upcoming commitment. The GUI's "My duties" list, over JSON."""

    assignment_id: int
    event: EventOut
    slot_id: int
    slot_name: str
    open_sub_request_id: int | None  # a substitute call you already opened


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


class SimilarEventOut(BaseModel):
    """An advisory double-booking hit. `title` is null when the colliding event
    belongs to a team outside the caller's view: the when and where is the
    warning, the details stay that team's."""

    starts_at: datetime
    ends_at: datetime
    location: str
    team_path: str
    title: str | None


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
