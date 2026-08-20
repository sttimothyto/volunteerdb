import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, UUID, Range
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

# One mechanism for every closed set of values: a native PostgreSQL enum, with
# the column typed as the Python enum so a typo is a failure at assignment
# rather than at flush. Earlier revisions used varchar + CHECK for these,
# reasoning that adding a value would need ALTER TYPE — but `ALTER TYPE … ADD
# VALUE` has existed since 9.1, and the one revision that did widen a set showed
# the CHECK path is no cheaper: it needed DROP + CREATE with the value list
# duplicated in the migration and in this file. Every member is a StrEnum, so `event.status ==
# "cancelled"` still reads true and nothing had to change at the comparison
# sites.
team_role_enum = sa.Enum(TeamRole, name="team_role")


class ProposalStatus(enum.StrEnum):
    open = "open"
    appointed = "appointed"
    cancelled = "cancelled"


class FieldType(enum.StrEnum):
    text = "text"
    number = "number"
    select = "select"
    date = "date"
    checkbox = "checkbox"
    integer = "integer"
    decimal = "decimal"
    timestamp = "timestamp"
    timestamptz = "timestamptz"
    time = "time"
    interval = "interval"
    uuid = "uuid"


FIELD_TYPE_LABELS: dict[FieldType, str] = {
    FieldType.text: "Text",
    FieldType.number: "Number",
    FieldType.select: "Choice",
    FieldType.date: "Date",
    FieldType.checkbox: "Checkbox",
    FieldType.integer: "Integer",
    FieldType.decimal: "Decimal",
    FieldType.timestamp: "Timestamp",
    FieldType.timestamptz: "Timestamp (with zone)",
    FieldType.time: "Time",
    FieldType.interval: "Duration",
    FieldType.uuid: "UUID",
}


class PageStatus(enum.StrEnum):
    """State of a team's cached public page (services/pages.py)."""

    pending = "pending"  # a doc URL is set, nothing fetched yet
    ok = "ok"
    error = "error"  # the last fetch failed; the previous html is still served


class SyncStatus(enum.StrEnum):
    """Outcome of a team's last Drive roster sync (jobs/drive_sync.py)."""

    applied = "applied"
    unchanged = "unchanged"  # the sheet was not newer than the last mark
    new = "new"  # a sheet was bootstrapped for a team that had none
    error = "error"


class NotificationStage(enum.StrEnum):
    """A one-shot notice, recorded once it has gone out (models.Notification).

    Named for what the reader is being told, not for when: `event_week` is "an
    event you serve at is coming up", whatever window the job decided that means.
    """

    event_scheduled = "event_scheduled"  # a manager put you on an event
    event_week = "event_week"  # an event you serve at is within the week
    event_day = "event_day"  # it starts tomorrow
    roll_added = "roll_added"  # you were added to a proposal's voting roll
    voting_open = "voting_open"  # voting began on a proposal you may vote in


class EventStatus(enum.StrEnum):
    scheduled = "scheduled"
    cancelled = "cancelled"


class AssignmentKind(enum.StrEnum):
    """How an event assignment came to be — provenance only, no logic
    branches on it."""

    signup = "signup"  # the volunteer signed themself up
    assigned = "assigned"  # a manager scheduled them
    sub = "sub"  # they claimed a substitution request


class SubRequestStatus(enum.StrEnum):
    open = "open"
    claimed = "claimed"
    cancelled = "cancelled"


notification_stage_enum = sa.Enum(NotificationStage, name="notification_stage")
proposal_status_enum = sa.Enum(ProposalStatus, name="proposal_status")
field_type_enum = sa.Enum(FieldType, name="custom_field_type")
event_status_enum = sa.Enum(EventStatus, name="event_status")
assignment_kind_enum = sa.Enum(AssignmentKind, name="assignment_kind")
sub_request_status_enum = sa.Enum(SubRequestStatus, name="sub_request_status")
page_status_enum = sa.Enum(PageStatus, name="page_status")
sync_status_enum = sa.Enum(SyncStatus, name="sync_status")

# sys_period marks when this row version became current; history triggers close it
SYS_PERIOD_DEFAULT = sa.text("tstzrange(clock_timestamp(), NULL)")


class Volunteer(Base):
    __tablename__ = "volunteer"
    # serves every list's ORDER BY last_name, first_name
    __table_args__ = (
        sa.Index("ix_volunteer_name", "last_name", "first_name"),
        # services.volunteers folds case on write; this is what stops a row
        # written any other way from hiding from every lookup, which all fold too
        sa.CheckConstraint(
            "email IS NULL OR email = lower(email)", name="ck_volunteer_email_lower"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(sa.String(100))
    last_name: Mapped[str] = mapped_column(sa.String(100))
    # Indexed on lower(email), not the raw column: every lookup in the codebase
    # folds case (services/volunteers.find_by_email), so a plain btree could
    # never be used. The functional index lives in the migration — SQLAlchemy
    # cannot express it on a mapped column.
    email: Mapped[str | None] = mapped_column(sa.String(255))
    phone: Mapped[str | None] = mapped_column(sa.String(50))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    # No updated_at: lower(sys_period) IS the last-modified time, maintained by
    # the versioning trigger on every write. A second column meant the same
    # thing only as long as every write went through the ORM — a Core UPDATE
    # left it stale — and nothing ever read it.
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
        # the unique above leads with parent_team_id, so it cannot serve a
        # lookup BY name — which is how the clergy voting roll is built
        # (services/elections.py) and how every listing orders
        sa.Index("ix_team_name", "name"),
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
    # How work-heavy this ministry is, for workload scores. NOT NULL: a weight
    # of 0 already meant "excluded", so a NULL that also meant 0 was a third
    # state with no distinct behaviour — and a coalesce at every read.
    workload_weight: Mapped[Decimal] = mapped_column(
        sa.Numeric(8, 2), default=Decimal(0), server_default=sa.text("0")
    )
    # public Google Doc used as the team's volunteer home page (services/pages.py).
    # keep this the LAST column so the history twin's order matches the DB
    home_doc_url: Mapped[str | None] = mapped_column(sa.String(500))


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (sa.UniqueConstraint("volunteer_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # no index=: the (volunteer_id, team_id) unique above leads with this column,
    # so a separate btree on it was a strict prefix — same lookups, twice the
    # write cost, which is why only the unique remains
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE")
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
            "nomination_deadline < voting_deadline",
            name="ck_proposal_deadlines",
        ),
        # at most one OPEN proposal per seat
        # the literal is cast explicitly: an index predicate comparing an enum
        # column to an untyped literal needs a cast PostgreSQL will not accept
        # as immutable there
        sa.Index(
            "uq_proposal_open",
            "team_id",
            "role",
            unique=True,
            postgresql_where=sa.text("status = 'open'::proposal_status"),
        ),
        # the ORDER BY of both list_proposals queries
        sa.Index("ix_proposal_created_at", "created_at"),
        # withdrawing a nominee deletes a proposal_candidate row, and this FK is
        # ON DELETE SET NULL — unindexed it made every withdrawal seq-scan
        sa.Index("ix_proposal_appointed_candidate", "appointed_candidate_id"),
        # an appointed proposal names its candidate and records the decision;
        # an open one has done neither
        # A decided proposal records WHEN. Not who, and not which candidate:
        # decided_by and appointed_candidate_id are both ON DELETE SET NULL, so
        # deleting an admin's account or withdrawing the appointee must not make
        # the decision unstorable — it only makes it less well attributed.
        sa.CheckConstraint(
            "(status = 'open') = (decided_at IS NULL)",
            name="ck_proposal_decision",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[TeamRole] = mapped_column(team_role_enum)
    status: Mapped[ProposalStatus] = mapped_column(
        proposal_status_enum,
        default=ProposalStatus.open,
        server_default=ProposalStatus.open.value,
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
        # the target of proposal_ballot's composite FK: it is what makes
        # "this ballot's candidate belongs to this ballot's proposal" a
        # constraint rather than a comment. Costs nothing — id is already unique.
        sa.UniqueConstraint("id", "proposal_id", name="uq_candidate_proposal"),
        # deleting a volunteer cascades here, and the column was unindexed
        sa.Index("ix_proposal_candidate_volunteer_id", "volunteer_id"),
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
        # as on proposal_candidate: the target of proposal_ballot's composite FK
        sa.UniqueConstraint("id", "proposal_id", name="uq_voter_proposal"),
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
    # What this voter has already been told lives in models.Notification, keyed
    # by (voter, stage) — it used to be two columns here, and a third notice
    # would have meant a third.


class ProposalBallot(Base):
    """One voter's 0-5 score for one candidate. Ballots are secret: scores
    leave the elections service only as aggregates, and the score column is
    redacted from audit logs (audit.REDACTED_COLUMNS).
    """

    __tablename__ = "proposal_ballot"
    __table_args__ = (
        sa.UniqueConstraint("voter_id", "candidate_id", name="uq_proposal_ballot"),
        sa.CheckConstraint("score BETWEEN 0 AND 5", name="ck_proposal_ballot_score"),
        # proposal_id is denormalized so tally/turnout are one query. These two
        # composite FKs are what keep it honest: the voter and the candidate must
        # belong to the SAME proposal this row claims. It used to be a comment
        # saying the service guarantees it — and a ballot cast against another
        # proposal's candidate would have counted in this one's tally.
        sa.ForeignKeyConstraint(
            ["voter_id", "proposal_id"],
            ["proposal_voter.id", "proposal_voter.proposal_id"],
            name="fk_ballot_voter_proposal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "proposal_id"],
            ["proposal_candidate.id", "proposal_candidate.proposal_id"],
            name="fk_ballot_candidate_proposal",
            ondelete="CASCADE",
        ),
        sa.Index("ix_proposal_ballot_proposal_id", "proposal_id"),
        sa.Index("ix_proposal_ballot_candidate_id", "candidate_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(sa.Integer)
    voter_id: Mapped[int] = mapped_column(sa.Integer)
    candidate_id: Mapped[int] = mapped_column(sa.Integer)
    score: Mapped[int] = mapped_column(sa.SmallInteger)
    # no onupdate=: cast_ballot writes through a Core ON CONFLICT upsert that
    # sets this explicitly, so the declarative hook never fired
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


class Event(Base):
    """One occasion a team serves: a Mass, a fundraiser shift, a task-force
    work day. Always attached to exactly ONE team — a parish-wide occasion
    gets its own task-force team first, so rosters, permissions and mail
    audiences all reuse the team machinery.

    "Past" derives from ends_at against the clock (the elections phase_of
    idiom), never stored; status records only the cancellation decision.

    Not system-versioned (like proposal): workflow data whose lifecycle is
    self-recorded in status/created_at/cancelled_at.
    """

    __tablename__ = "event"
    __table_args__ = (
        sa.CheckConstraint("starts_at < ends_at", name="ck_event_times"),
        # serves the team page's and /events' team-scoped upcoming lists
        sa.Index("ix_event_team_starts", "team_id", "starts_at"),
        # serves the reminder job's and /events' date-window scans
        sa.Index("ix_event_starts_at", "starts_at"),
        # serves sign_up_series's "future rows of this series" sweep
        sa.Index(
            "ix_event_series",
            "series_id",
            postgresql_where=sa.text("series_id IS NOT NULL"),
        ),
        # ends_at is a WHERE predicate in five places (my_upcoming,
        # claimable_subs, hours_for_volunteer, the reminder job, task-force
        # teardown) and was unindexed
        sa.Index("ix_event_ends_at", "ends_at"),
        # jobs/calendar_sync.py scans "ends_at > horizon OR google_event_id IS
        # NOT NULL" every 30 minutes; with this and the index above, that
        # becomes a bitmap OR instead of a seq scan
        sa.Index(
            "ix_event_google_id",
            "google_event_id",
            postgresql_where=sa.text("google_event_id IS NOT NULL"),
        ),
        # cancelled is not a free-floating status: it has a moment
        sa.CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL)",
            name="ck_event_cancelled_at",
        ),
        # the meta team must never BE the owner: teardown puts the event back on
        # the owner and then deletes the meta team, so an equal pair would leave
        # the event on a team about to disappear
        sa.CheckConstraint(
            "task_force_team_id IS NULL OR task_force_team_id <> owner_team_id",
            name="ck_event_task_force_teams",
        ),
        # one event per meta team: a task force exists for exactly one occasion
        sa.UniqueConstraint("task_force_team_id", name="uq_event_task_force_team"),
        # a cascade-delete path from team, and the column the teardown sweep reads
        sa.Index(
            "ix_event_owner_team",
            "owner_team_id",
            postgresql_where=sa.text("owner_team_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # no separate index: ix_event_team_starts leads with team_id
    team_id: Mapped[int] = mapped_column(sa.ForeignKey("team.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str | None] = mapped_column(sa.Text)
    location: Mapped[str | None] = mapped_column(sa.String(200))
    starts_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True))
    status: Mapped[EventStatus] = mapped_column(
        event_status_enum,
        default=EventStatus.scheduled,
        server_default=EventStatus.scheduled.value,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    # calendar-sync bookkeeping, owned by jobs/calendar_sync.py — never
    # user-editable. NULL google_event_id = not (yet) on the parish calendar;
    # the fingerprint is a hash of the last-pushed payload (change detection).
    google_event_id: Mapped[str | None] = mapped_column(sa.String(1024))
    google_fingerprint: Mapped[str | None] = mapped_column(sa.String(64))
    # stamped by create_event on weekly repeats; NULL = standalone. Not a
    # recurrence engine: the id exists solely so a sign-up can copy itself
    # onto the later weeks of the same series.
    series_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # --- task force (services/task_force.py) ---------------------------------
    # When several teams staff one occasion, an auto-created "task force" team
    # holds the union of their rosters and team_id above points at it, with the
    # real owner parked here until teardown. Both NULL means one team staffs
    # this event alone, which is the ordinary case.
    #
    # ON DELETE SET NULL on both, and that is the point of them living here.
    # They used to be a side table whose team_id cascaded, so deleting the meta
    # team deleted the row — which meant teardown had to repoint the event and
    # FLUSH *before* the delete, or event.team_id's own cascade took the event
    # and its whole attendance record with it. An ordering requirement that
    # load-bearing is better expressed as a column that simply goes NULL.
    task_force_team_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("team.id", ondelete="SET NULL")
    )
    owner_team_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("team.id", ondelete="SET NULL")
    )


class EventSlot(Base):
    """A named position to fill at an event ("Lector", "Greeter — main door").
    capacity NULL means unlimited — the rule that lets one schema serve both
    staffed liturgies and open attendance-style gatherings (create_event with
    no slots makes a single unlimited "Volunteers" slot)."""

    __tablename__ = "event_slot"
    __table_args__ = (
        # also serves event-scoped slot reads (leads with event_id)
        sa.UniqueConstraint("event_id", "name", name="uq_event_slot_name"),
        # the target of event_assignment's composite FK — see the note there
        sa.UniqueConstraint("id", "event_id", name="uq_slot_event"),
        sa.CheckConstraint(
            "capacity IS NULL OR capacity >= 1", name="ck_event_slot_capacity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(sa.ForeignKey("event.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(sa.String(100))
    capacity: Mapped[int | None] = mapped_column(sa.SmallInteger)
    position: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))


class EventAssignment(Base):
    """One volunteer filling one slot of one event.

    Attendance is derived, not entered: once the event has ended (and was not
    cancelled) an assignment counts as attended for the scheduled duration
    unless a manager recorded an exception in attended_override /
    hours_override (services/events.effective).
    """

    __tablename__ = "event_assignment"
    __table_args__ = (
        # one slot per person per event; also serves event-scoped reads
        sa.UniqueConstraint("event_id", "volunteer_id", name="uq_event_assignment"),
        sa.CheckConstraint(
            "hours_override IS NULL OR hours_override >= 0",
            name="ck_event_assignment_hours",
        ),
        # event_id is denormalized (so the unique above and every per-event read
        # are direct). This composite FK is what keeps it true: the slot must
        # belong to the event the row claims. Otherwise somebody could hold slot
        # A of event B while uq_event_assignment guarded the wrong pairing.
        sa.ForeignKeyConstraint(
            ["slot_id", "event_id"],
            ["event_slot.id", "event_slot.event_id"],
            name="fk_assignment_slot_event",
            ondelete="CASCADE",
        ),
        sa.Index("ix_event_assignment_slot_id", "slot_id"),
        sa.Index("ix_event_assignment_volunteer_id", "volunteer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # slot_id/event_id carry no single-column FK of their own: the composite
    # one in __table_args__ covers both and constrains them together
    slot_id: Mapped[int] = mapped_column(sa.Integer)
    event_id: Mapped[int] = mapped_column(sa.ForeignKey("event.id", ondelete="CASCADE"))
    # index serves "my upcoming assignments" and the hours summary
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE")
    )
    kind: Mapped[AssignmentKind] = mapped_column(assignment_kind_enum)
    assigned_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    # manager-recorded exceptions to auto attendance; NULL = auto
    attended_override: Mapped[bool | None]
    hours_override: Mapped[Decimal | None] = mapped_column(sa.Numeric(5, 2))
    # Reminder PREFERENCES, chosen at sign-up and pre-checked: an opted-out
    # stage simply never fires. These stay on the assignment because they are
    # settings, not records — what has already been *sent* lives in
    # models.Notification, keyed by (assignment, stage).
    #
    # Declared after the overrides because the revision that added them appended
    # them there; see AppUser's docstring for why declaration order is kept
    # honest against the deployed order.
    notify_7d: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    notify_24h: Mapped[bool] = mapped_column(default=True, server_default=sa.true())


class EventTaskForceSource(Base):
    """One team whose roster was copied into an event's task force (the owner
    team is always a source too).

    The only part of the task force that still needs a table of its own: the
    event carries which meta team and which owner (models.Event), and this
    carries the list of contributors, which is many per event.
    """

    __tablename__ = "event_task_force_source"

    # (event, team) is the row's identity, so it is the primary key — the
    # surrogate id was a second one, and its only reader was an existence probe
    event_id: Mapped[int] = mapped_column(
        sa.ForeignKey("event.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        sa.ForeignKey("team.id", ondelete="CASCADE"), primary_key=True
    )


class EventRsvp(Base):
    """One volunteer's availability answer for one event; no row = not
    answered. Managers assign from this pool. Not a commitment — the
    assignment is."""

    __tablename__ = "event_rsvp"
    __table_args__ = (
        sa.UniqueConstraint("event_id", "volunteer_id", name="uq_event_rsvp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # no separate index: uq_event_rsvp leads with event_id
    event_id: Mapped[int] = mapped_column(sa.ForeignKey("event.id", ondelete="CASCADE"))
    # index serves "my RSVPs" on the events page
    volunteer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="CASCADE"), index=True
    )
    available: Mapped[bool]
    note: Mapped[str | None] = mapped_column(sa.String(200))
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    # no onupdate=: set_rsvp upserts through Core and sets this explicitly
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


class EventSubRequest(Base):
    """An assignee's open call for a substitute. Teammates are emailed when it
    opens; the first to claim takes over the assignment (the assignment row
    itself moves to the claimant — claimed_by_volunteer_id records who).
    Open requests are resolved by the event's cancellation.

    Not system-versioned (like proposal): workflow data whose lifecycle is
    self-recorded in status/created_at/resolved_at.
    """

    __tablename__ = "event_sub_request"
    __table_args__ = (
        # at most one OPEN request per assignment
        sa.Index(
            "uq_event_sub_request_open",
            "assignment_id",
            unique=True,
            postgresql_where=sa.text("status = 'open'::sub_request_status"),
        ),
        # open means unresolved; claimed means resolved AND by somebody
        sa.CheckConstraint(
            "(status = 'open') = (resolved_at IS NULL)",
            name="ck_sub_request_resolution",
        ),
        # No "claimed implies a claimant": claimed_by_volunteer_id is ON DELETE
        # SET NULL, so deleting the volunteer who took the slot must not make the
        # historical request unstorable. The reverse direction is the real
        # invariant — a claimant means it was claimed.
        sa.CheckConstraint(
            "claimed_by_volunteer_id IS NULL OR status = 'claimed'",
            name="ck_sub_request_claimant",
        ),
        # deleting a volunteer sets this NULL, and the column was unindexed
        sa.Index("ix_event_sub_request_claimant", "claimed_by_volunteer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # no index=: uq_event_sub_request_open covers it, and every assignment_id
    # predicate in the services carries `status = 'open'` alongside
    assignment_id: Mapped[int] = mapped_column(
        sa.ForeignKey("event_assignment.id", ondelete="CASCADE")
    )
    requested_by: Mapped[int | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(sa.String(200))
    status: Mapped[SubRequestStatus] = mapped_column(
        sub_request_status_enum,
        default=SubRequestStatus.open,
        server_default=SubRequestStatus.open.value,
    )
    claimed_by_volunteer_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))


class Notification(Base):
    """One notice already sent, so the nightly jobs never send it twice.

    Three hand-rolled versions of this used to sit as columns on the rows they
    described: `assigned_notified_at` / `reminded_7d_at` / `reminded_24h_at` on
    event_assignment, and `added_notified_at` / `voting_notified_at` on
    proposal_voter. Every new notice meant two more columns and a migration —
    one of them added four at once — and the "who still needs telling" query had
    to OR across all of them — a shape no plain index can serve.

    Deliberately NOT polymorphic. A single `(entity_type, entity_id)` pair would
    have been shorter and would have thrown away the thing the old columns got
    for free: deleting an assignment took its stamps with it. Two nullable
    foreign keys keep that, keep referential integrity, and let the CHECK below
    insist on exactly one subject per row.

    Not system-versioned: bookkeeping, not parish data. A failed send simply
    writes no row and the next night tries again.
    """

    __tablename__ = "notification"
    __table_args__ = (
        # one notice of each kind per thing, which is what makes a re-run safe
        sa.UniqueConstraint(
            "assignment_id", "stage", name="uq_notification_assignment"
        ),
        sa.UniqueConstraint("voter_id", "stage", name="uq_notification_voter"),
        # exactly one subject: `<>` on two IS NULL tests is XOR
        sa.CheckConstraint(
            "(assignment_id IS NULL) <> (voter_id IS NULL)",
            name="ck_notification_subject",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[NotificationStage] = mapped_column(notification_stage_enum)
    assignment_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("event_assignment.id", ondelete="CASCADE")
    )
    voter_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("proposal_voter.id", ondelete="CASCADE")
    )
    sent_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )


class AppUser(Base):
    """A sign-in account. Deliberately separate from the volunteer it belongs
    to: most volunteers never sign in, and a login's team rights derive from its
    linked volunteer's memberships.

    The CHECKs below hold the three "set and cleared together" groups the
    columns document. Each was a comment the services honoured and the schema
    allowed the other way round — a token with no expiry, or an expiry with no
    token, would have read as a live link or a dead one depending on which
    function looked.

    The declaration order is the *deployed* order, not the logical one: the
    invite expiry, the confidentiality stamp and the address-change trio were
    each appended by a later revision, so that is where they physically sit in
    every live database. Keeping the two in step is what lets a fresh
    database and a migrated one be diffed against each other, which is how the
    squashed 0001 was verified. Group members are noted in their comments
    instead.
    """

    __tablename__ = "app_user"
    __table_args__ = (
        sa.CheckConstraint(
            "(invite_token IS NULL) = (invite_expires_at IS NULL)",
            name="ck_app_user_invite_pair",
        ),
        sa.CheckConstraint(
            "(pending_email IS NULL) = (email_change_token IS NULL)"
            " AND (pending_email IS NULL) = (email_change_expires_at IS NULL)",
            name="ck_app_user_email_change_triple",
        ),
        sa.CheckConstraint(
            "(otp_hash IS NULL) = (otp_expires_at IS NULL)",
            name="ck_app_user_otp_pair",
        ),
        sa.CheckConstraint("email = lower(email)", name="ck_app_user_email_lower"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("volunteer.id", ondelete="SET NULL"), unique=True
    )
    email: Mapped[str] = mapped_column(sa.String(255), unique=True)
    # adding a secret column? add it to audit.REDACTED_COLUMNS
    password_hash: Mapped[str | None] = mapped_column(sa.String(255))
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    api_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    # Only the SHA-256 digest of the invite link is stored, as for api_token, so
    # a read of this table hands out nothing. Paired with invite_expires_at
    # below — set and cleared together, and CHECKed.
    invite_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    last_login_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    # an active email OTP: argon2 hash, because six digits is a small space
    otp_hash: Mapped[str | None] = mapped_column(sa.String(255))
    otp_sent_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    otp_expires_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    otp_attempts: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    # The other half of invite_token. A token whose expiry has passed
    # (or was never recorded) is dead — see services/users.invite_live.
    invite_expires_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True)
    )
    # When the person accepted the confidentiality notice while
    # redeeming their invite. NULL: the account predates the notice.
    confidentiality_agreed_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True)
    )
    # An address change waits here until the new address opens its
    # link, so nothing on file moves before somebody proves they read mail
    # there. Set and cleared as a triple, and CHECKed as one; the address itself
    # is deliberately not unique until it lands, because two people may ask and
    # only the first to confirm gets it. The token is a digest, like the others.
    pending_email: Mapped[str | None] = mapped_column(sa.String(255))
    email_change_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    email_change_expires_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True)
    )


class CustomFieldDef(Base):
    """Admin-defined volunteer field. Values live in Volunteer.custom under `key`.

    Not system-versioned (like app_user): historical volunteer snapshots render
    against the *current* definitions.
    """

    __tablename__ = "custom_field_def"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(50), unique=True)  # immutable slug
    label: Mapped[str] = mapped_column(sa.String(100))
    field_type: Mapped[FieldType] = mapped_column(field_type_enum)
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
    # no onupdate=: workload.set_config upserts through Core and sets this
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
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
    status: Mapped[PageStatus] = mapped_column(
        page_status_enum,
        default=PageStatus.pending,
        server_default=PageStatus.pending.value,
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
    last_status: Mapped[SyncStatus | None] = mapped_column(sync_status_enum)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now()
    )
    # An admin has asked to repoint this team at a different spreadsheet
    # (services.teams.request_roster_sheet). Held BESIDE file_id rather than
    # over it: nothing in the app can reach Drive, so the link is only checked
    # by the next nightly sync, and a link that turns out to be invisible or
    # malformed must not have cost the team the sheet it already had.
    requested_file_id: Mapped[str | None] = mapped_column(sa.String(128), unique=True)
    # Which side wins the first sync after the switch: False regenerates the
    # newly linked sheet from the database (the default the dialog offers, and
    # the one that cannot lose parish data), True imports its rows through the
    # importer's usual layout checks and removal thresholds.
    requested_import: Mapped[bool] = mapped_column(
        default=False, server_default=sa.false()
    )


class JobRun(Base):
    """Nightly-job bookkeeping for the in-app scheduler (volunteerdb.scheduler).

    One row per job, existing solely so a restart cannot skip a night: the
    scheduler runs any job whose last_success_on predates the parish today.
    Written via a Core ON CONFLICT upsert (the audit listeners still log the
    write, as they do every write).

    Not system-versioned: scheduler state, not parish data.
    """

    __tablename__ = "job_run"

    job_name: Mapped[str] = mapped_column(sa.String(100), primary_key=True)
    last_success_on: Mapped[date | None] = mapped_column(sa.Date)  # parish date
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True)
    )
    # Written, never read by the app: it is for whoever is looking at a job that
    # misbehaved, one SELECT away, after the alert mail has already gone out.
    # Kept deliberately — a nightly job's last exit code costs one integer and
    # is the first thing a postmortem wants.
    last_exit_code: Mapped[int | None] = mapped_column(sa.Integer)


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

# the timeline view filters membership history by volunteer. The one other
# expression-only index, `ix_volunteer_email_lower`, cannot be declared on a
# mapped column either and lives in the migration alone.
sa.Index("ix_membership_history_volunteer_id", membership_history.c.volunteer_id)

HISTORY_TABLES: dict[type[Base], sa.Table] = {
    Volunteer: volunteer_history,
    Team: team_history,
    Membership: membership_history,
}
