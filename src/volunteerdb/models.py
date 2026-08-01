import enum
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TeamRole(enum.StrEnum):
    leader = "leader"
    second = "second"
    core = "core"
    member = "member"


ROLE_LABELS: dict[TeamRole, str] = {
    TeamRole.leader: "Ministry leader",
    TeamRole.second: "Second-in-command",
    TeamRole.core: "Core team member",
    TeamRole.member: "Member",
}

team_role_enum = sa.Enum(TeamRole, name="team_role")


class ProposalStatus(enum.StrEnum):
    proposed = "proposed"
    accepted = "accepted"
    declined = "declined"
    withdrawn = "withdrawn"


PROPOSAL_STATUS_LABELS: dict[ProposalStatus, str] = {
    ProposalStatus.proposed: "Proposed",
    ProposalStatus.accepted: "Accepted",
    ProposalStatus.declined: "Declined",
    ProposalStatus.withdrawn: "Withdrawn",
}


class FieldType(enum.StrEnum):
    text = "text"
    number = "number"
    select = "select"
    date = "date"
    checkbox = "checkbox"


FIELD_TYPE_LABELS: dict[FieldType, str] = {
    FieldType.text: "Text",
    FieldType.number: "Number",
    FieldType.select: "Choice",
    FieldType.date: "Date",
    FieldType.checkbox: "Checkbox",
}

# sys_period marks when this row version became current; history triggers close it
SYS_PERIOD_DEFAULT = sa.text("tstzrange(clock_timestamp(), NULL)")


class Volunteer(Base):
    __tablename__ = "volunteer"
    # serves every list's ORDER BY last_name, first_name (rev 0005)
    __table_args__ = (sa.Index("ix_volunteer_name", "last_name", "first_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(sa.String(100))
    last_name: Mapped[str] = mapped_column(sa.String(100))
    email: Mapped[str | None] = mapped_column(sa.String(255), index=True)
    phone: Mapped[str | None] = mapped_column(sa.String(50))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
    sys_period: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, server_default=SYS_PERIOD_DEFAULT
    )
    # admin-defined custom field values, keyed by CustomFieldDef.key;
    # keep this the LAST column so the history twin's order matches the DB
    custom: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Team(Base):
    __tablename__ = "team"
    __table_args__ = (
        sa.UniqueConstraint(
            "parent_team_id", "name", postgresql_nulls_not_distinct=True
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    parent_team_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("team.id", ondelete="RESTRICT")
    )
    description: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    sys_period: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, server_default=SYS_PERIOD_DEFAULT
    )
    # optional workload weight ("how work-heavy is this ministry"); NULL counts as 0.
    # keep this the LAST column so the history twin's order matches the DB
    workload_weight: Mapped[Decimal | None] = mapped_column(sa.Numeric(8, 2))


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (sa.UniqueConstraint("volunteer_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE"), index=True
    )
    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[TeamRole] = mapped_column(team_role_enum)
    joined_on: Mapped[date | None]
    notes: Mapped[str | None] = mapped_column(sa.Text)
    sys_period: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, server_default=SYS_PERIOD_DEFAULT
    )


class Proposal(Base):
    """A planner's suggestion to fill a vacant role on a team.

    Not system-versioned (like custom_field_def): workflow data whose
    lifecycle is already self-recorded in status/created_at/decided_*.
    The membership an accepted proposal creates *is* versioned.
    """

    __tablename__ = "proposal"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'declined', 'withdrawn')",
            name="ck_proposal_status",
        ),
        # at most one OPEN proposal per (team, role, volunteer)
        sa.Index(
            "uq_proposal_open",
            "team_id",
            "role",
            "volunteer_id",
            unique=True,
            postgresql_where=sa.text("status = 'proposed'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), index=True
    )
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE")
    )
    role: Mapped[TeamRole] = mapped_column(team_role_enum)
    # plain string + CHECK, not a PG enum, so adding statuses never needs ALTER TYPE
    status: Mapped[str] = mapped_column(
        sa.String(20), default=ProposalStatus.proposed.value, server_default="proposed"
    )
    note: Mapped[str | None] = mapped_column(sa.Text)
    proposed_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="SET NULL"), unique=True
    )
    email: Mapped[str] = mapped_column(sa.String(255), unique=True)
    # adding a secret column? add it to audit.REDACTED_COLUMNS
    password_hash: Mapped[str | None] = mapped_column(sa.String(255))
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    api_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    invite_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    otp_hash: Mapped[str | None] = mapped_column(sa.String(255))
    otp_sent_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    otp_expires_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    otp_attempts: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    last_login_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


class CustomFieldDef(Base):
    """Admin-defined volunteer field. Values live in Volunteer.custom under `key`.

    Not system-versioned (like app_user): historical volunteer snapshots render
    against the *current* definitions.
    """

    __tablename__ = "custom_field_def"
    __table_args__ = (
        sa.CheckConstraint(
            "field_type IN ('text', 'number', 'select', 'date', 'checkbox')",
            name="ck_custom_field_def_field_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(50), unique=True)  # immutable slug
    label: Mapped[str] = mapped_column(sa.String(100))
    # plain string + CHECK, not a PG enum, so adding types never needs ALTER TYPE
    field_type: Mapped[str] = mapped_column(sa.String(20))
    options: Mapped[list | None] = mapped_column(JSONB)  # select choices
    show_in_list: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    position: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


class AppSetting(Base):
    """Key/JSON application settings (e.g. workload config). Not versioned."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(sa.String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class VolunteerPhoto(Base):
    """Normalized headshot: 400x400 JPEG, at most services.photos.PHOTO_MAX_BYTES.

    Not system-versioned (like custom_field_def): photos are current-state only,
    so as-of views show the current photo.
    """

    __tablename__ = "volunteer_photo"

    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE"), primary_key=True
    )
    image: Mapped[bytes] = mapped_column(sa.LargeBinary)
    content_type: Mapped[str] = mapped_column(
        sa.String(50), default="image/jpeg", server_default="image/jpeg"
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


def _make_history_table(live: sa.Table) -> sa.Table:
    """History twin: live columns (no PK/defaults) + audit columns, no FKs so
    archived rows survive deletion of whatever they referenced."""
    return sa.Table(
        f"{live.name}_history",
        Base.metadata,
        *[sa.Column(c.name, c.type) for c in live.columns],
        sa.Column("changed_by", sa.Integer),
        sa.Column("op", sa.CHAR(1)),
        sa.Index(f"ix_{live.name}_history_id", "id"),
        sa.Index(
            f"ix_{live.name}_history_sys_period", "sys_period", postgresql_using="gist"
        ),
    )


volunteer_history = _make_history_table(Volunteer.__table__)
team_history = _make_history_table(Team.__table__)
membership_history = _make_history_table(Membership.__table__)

# the timeline view filters membership history by volunteer (rev 0005; the
# trigram search indexes from that revision are expression-only and live
# solely in the migration)
sa.Index("ix_membership_history_volunteer_id", membership_history.c.volunteer_id)

HISTORY_TABLES: dict[type[Base], sa.Table] = {
    Volunteer: volunteer_history,
    Team: team_history,
    Membership: membership_history,
}
