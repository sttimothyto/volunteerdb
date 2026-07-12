import enum
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
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
