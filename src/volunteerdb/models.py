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
    open = "open"
    appointed = "appointed"
    cancelled = "cancelled"


PROPOSAL_STATUS_LABELS: dict[ProposalStatus, str] = {
    ProposalStatus.open: "Open",
    ProposalStatus.appointed: "Appointed",
    ProposalStatus.cancelled: "Cancelled",
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
    # optional workload weight ("how work-heavy is this ministry"); NULL counts as 0
    workload_weight: Mapped[Decimal | None] = mapped_column(sa.Numeric(8, 2))
    # public Google Doc used as the team's volunteer home page (services/pages.py).
    # keep this the LAST column so the history twin's order matches the DB
    home_doc_url: Mapped[str | None] = mapped_column(sa.String(500))


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
    sys_period: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, server_default=SYS_PERIOD_DEFAULT
    )


class Proposal(Base):
    """One run at filling a (team, role) seat: candidates are nominated until
    the nomination deadline, the voting roll scores them (STAR) until the
    voting deadline, then a manager appoints — or starts a new round. The
    phase within an open proposal derives from today's date, never stored.

    Not system-versioned (like custom_field_def): workflow data whose
    lifecycle is already self-recorded in status/created_at/decided_*.
    The membership an appointment creates *is* versioned.
    """

    __tablename__ = "proposal"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('open', 'appointed', 'cancelled')",
            name="ck_proposal_status",
        ),
        sa.CheckConstraint(
            "nomination_deadline < voting_deadline",
            name="ck_proposal_deadlines",
        ),
        # at most one OPEN proposal per seat
        sa.Index(
            "uq_proposal_open",
            "team_id",
            "role",
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[TeamRole] = mapped_column(team_role_enum)
    # plain string + CHECK, not a PG enum, so adding statuses never needs ALTER TYPE
    status: Mapped[str] = mapped_column(
        sa.String(20), default=ProposalStatus.open.value, server_default="open"
    )
    notes: Mapped[str | None] = mapped_column(sa.Text)
    # last day to nominate / last day to vote, inclusive, in the parish's day
    # (settings().timezone) — the phase boundaries of an open proposal
    nomination_deadline: Mapped[date]
    voting_deadline: Mapped[date]
    appointed_candidate_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey(
            "proposal_candidate.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_proposal_appointed_candidate",
        )
    )
    created_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    decided_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))


class ProposalCandidate(Base):
    """A volunteer put forward for the seat, with the nominator's reasoning."""

    __tablename__ = "proposal_candidate"
    __table_args__ = (
        sa.UniqueConstraint(
            "proposal_id", "volunteer_id", name="uq_proposal_candidate"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        sa.ForeignKey("proposal.id", ondelete="CASCADE")
    )
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE")
    )
    note: Mapped[str | None] = mapped_column(sa.Text)
    nominated_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


class ProposalVoter(Base):
    """A member of the proposal's voting roll. Casting a ballot additionally
    requires an active account linked to the volunteer (AppUser.volunteer_id).
    """

    __tablename__ = "proposal_voter"
    __table_args__ = (
        sa.UniqueConstraint("proposal_id", "volunteer_id", name="uq_proposal_voter"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        sa.ForeignKey("proposal.id", ondelete="CASCADE")
    )
    # index serves load_actor's "which rolls am I on?" per-request lookup
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE"), index=True
    )
    added_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


class ProposalBallot(Base):
    """One voter's 0-5 score for one candidate. Ballots are secret: scores
    leave the planning service only as aggregates, and the score column is
    redacted from audit logs (audit.REDACTED_COLUMNS).
    """

    __tablename__ = "proposal_ballot"
    __table_args__ = (
        sa.UniqueConstraint("voter_id", "candidate_id", name="uq_proposal_ballot"),
        sa.CheckConstraint("score BETWEEN 0 AND 5", name="ck_proposal_ballot_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # denormalized so tally/turnout are one query; the service guarantees the
    # voter and candidate rows belong to this proposal
    proposal_id: Mapped[int] = mapped_column(
        sa.ForeignKey("proposal.id", ondelete="CASCADE"), index=True
    )
    voter_id: Mapped[int] = mapped_column(
        sa.ForeignKey("proposal_voter.id", ondelete="CASCADE")
    )
    candidate_id: Mapped[int] = mapped_column(
        sa.ForeignKey("proposal_candidate.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[int] = mapped_column(sa.SmallInteger)
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
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
    # Set and cleared together with invite_token; a token whose expiry has
    # passed (or was never recorded) is dead. See services/users.py.
    invite_expires_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True)
    )
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


class TeamPage(Base):
    """Sanitized HTML of the team's public Google Doc, fetched nightly (and on
    demand) by jobs.fetch_pages and served at /ministries/<slug>.html.

    Not system-versioned: a cache of an external document, current-state only.
    A failed fetch keeps the last good html alongside status='error'.
    """

    __tablename__ = "team_page"

    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), primary_key=True
    )
    html: Mapped[str | None] = mapped_column(sa.Text)
    fetched_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(
        sa.String(20), default="pending", server_default="pending"
    )
    error: Mapped[str | None] = mapped_column(sa.Text)


class TeamPageImage(Base):
    """Locally cached copy of an image embedded in the team's public Google
    Doc. The doc export hotlinks signed googleusercontent URLs that expire,
    so fetch_and_store downloads each image and rewrites the page html to
    /ministries/img/<team_id>/<seq>.

    Not system-versioned: a cache of external content, replaced wholesale
    whenever a successful fetch changed the page html (a failed fetch keeps
    the previous set, matching the kept html; an unchanged doc keeps the rows
    untouched — see services.pages.fetch_and_store).
    """

    __tablename__ = "team_page_image"

    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    image: Mapped[bytes] = mapped_column(sa.LargeBinary)
    content_type: Mapped[str] = mapped_column(sa.String(50))


class TeamSheet(Base):
    """Identity of the team's roster spreadsheet in Google Drive, maintained by
    jobs.drive_sync. The stable file_id (not the name) is what keeps the
    leader-facing sheet link working across team renames.

    Not system-versioned: a pointer to an external artifact, current-state only.
    """

    __tablename__ = "team_sheet"

    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[str | None] = mapped_column(sa.String(128), unique=True)
    file_name: Mapped[str | None] = mapped_column(sa.String(300))
    last_synced_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    last_status: Mapped[str | None] = mapped_column(sa.String(20))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
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
