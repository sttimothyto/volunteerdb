from datetime import date, datetime
from typing import Any

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
    workload_weight: float | None = None  # capacity weight; null = unweighted


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
    joined_on: date | None
    notes: str | None


class MembershipIn(BaseModel):
    volunteer_id: int
    team_id: int
    role: TeamRole
    joined_on: date | None = None
    notes: str | None = None


class RosterEntry(BaseModel):
    membership_id: int
    volunteer: VolunteerOut
    role: TeamRole
    role_label: str
    joined_on: date | None


# --- capacity ---


class BandOut(BaseModel):
    label: str
    color: str
    upper: float | None  # inclusive threshold; null = unbounded (last band)


class CapacityConfigOut(BaseModel):
    multipliers: dict[TeamRole, float]
    bands: list[BandOut]


class CapacityConfigIn(CapacityConfigOut):
    pass


class CapacityScoreOut(BaseModel):
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


# --- users ---


class UserOut(ORMModel):
    id: int
    email: str
    volunteer_id: int | None
    is_admin: bool
    is_active: bool
    has_password: bool = False
    invite_token: str | None = None
    last_login_at: datetime | None


class UserIn(BaseModel):
    email: EmailStr
    volunteer_id: int | None = None
    is_admin: bool = False
    password: str | None = None


class UserPatch(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


# --- auth ---


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    token: str


def role_label(role: TeamRole) -> str:
    return ROLE_LABELS[role]
