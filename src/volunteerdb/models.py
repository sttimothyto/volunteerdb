import enum
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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

# lower rank = more responsibility; used for sorting rosters and permission checks
ROLE_RANK: dict[TeamRole, int] = {role: i for i, role in enumerate(TeamRole)}

team_role_enum = sa.Enum(TeamRole, name="team_role")


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
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )
    sys_period: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, server_default=SYS_PERIOD_DEFAULT
    )
    # admin-defined custom field values, keyed by CustomFieldDef.key;
    # keep this the LAST column so the history twin's order matches the DB
    custom: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="volunteer", cascade="all, delete-orphan"
    )
    user: Mapped["AppUser | None"] = relationship(back_populates="volunteer")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Team(Base):
    __tablename__ = "team"
    __table_args__ = (
        sa.UniqueConstraint("parent_team_id", "name", postgresql_nulls_not_distinct=True),
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
    # optional capacity weight ("how work-heavy is this ministry"); NULL counts as 0.
    # keep this the LAST column so the history twin's order matches the DB
    workload_weight: Mapped[Decimal | None] = mapped_column(sa.Numeric(8, 2))

    parent: Mapped["Team | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Team"]] = relationship(back_populates="parent")
    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (sa.UniqueConstraint("volunteer_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE"), index=True
    )
    team_id: Mapped[int] = mapped_column(sa.ForeignKey("team.id", ondelete="CASCADE"), index=True)
    role: Mapped[TeamRole] = mapped_column(team_role_enum)
    joined_on: Mapped[date | None]
    notes: Mapped[str | None] = mapped_column(sa.Text)
    sys_period: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, server_default=SYS_PERIOD_DEFAULT
    )

    volunteer: Mapped[Volunteer] = relationship(back_populates="memberships")
    team: Mapped[Team] = relationship(back_populates="memberships")


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="SET NULL"), unique=True
    )
    email: Mapped[str] = mapped_column(sa.String(255), unique=True)
    password_hash: Mapped[str | None] = mapped_column(sa.String(255))
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    api_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    invite_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    last_login_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )

    volunteer: Mapped[Volunteer | None] = relationship(back_populates="user")


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
    """Key/JSON application settings (e.g. capacity config). Not versioned."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(sa.String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
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
        sa.Index(f"ix_{live.name}_history_sys_period", "sys_period", postgresql_using="gist"),
    )


volunteer_history = _make_history_table(Volunteer.__table__)
team_history = _make_history_table(Team.__table__)
membership_history = _make_history_table(Membership.__table__)

HISTORY_TABLES: dict[type[Base], sa.Table] = {
    Volunteer: volunteer_history,
    Team: team_history,
    Membership: membership_history,
}
