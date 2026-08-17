"""Scheduling subsystem: event, event_slot, event_assignment, event_rsvp,
event_sub_request

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-16

Events are occasions a team serves — a Mass, a fundraiser shift, a task-force
work day. Every event belongs to exactly one team (a parish-wide occasion
gets its own task-force team first), so rosters, permissions and mail
audiences reuse the team machinery. An event carries named slots with
capacity (NULL = unlimited; an event created without slots gets one
unlimited "Volunteers" slot). Volunteers RSVP per event (available /
unavailable) and sign up for slots; assignees who can no longer serve open a
substitution request that any teammate may claim — the claim moves the
assignment row to the claimant. Attendance is derived, not entered: an
assignment on a past, non-cancelled event counts as attended for the
scheduled duration unless a manager recorded attended_override /
hours_override.

None of the tables is system-versioned (like proposal and interest):
workflow data whose lifecycle is self-recorded in status / created_at /
cancelled_at / resolved_at, and the audit listeners log every write.
"Past" is derived from ends_at against the clock (the planning phase_of
idiom), never stored.

The per-assignment stamps assigned_notified_at / reminder_sent_at are read
and set only by jobs.event_reminders (the proposal-digest pattern): NULL
means not yet told, so a failed send retries just that person the next
night. Self sign-ups and sub claims are stamped at insert — the person acted
themselves; only manager assignments get the "you have been scheduled"
notice.

The partial unique index allows at most one OPEN substitution request per
assignment — the dedup that keeps repeat clicks from re-mailing the team.

New non-versioned tables only: no versioned-table columns move, so the
history-twin positional-parity rebuild recipe from revision 0002 does not
apply.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("location", sa.String(200)),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ends_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="scheduled"),
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "cancelled_by",
            sa.Integer,
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("starts_at < ends_at", name="ck_event_times"),
        sa.CheckConstraint(
            "status IN ('scheduled', 'cancelled')", name="ck_event_status"
        ),
    )
    # team-scoped upcoming lists (leads with team_id — no separate FK index)
    op.create_index("ix_event_team_starts", "event", ["team_id", "starts_at"])
    # the reminder job's and /events' date-window scans
    op.create_index("ix_event_starts_at", "event", ["starts_at"])

    op.create_table(
        "event_slot",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("capacity", sa.SmallInteger),  # NULL = unlimited
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        # also serves event-scoped slot reads (leads with event_id)
        sa.UniqueConstraint("event_id", "name", name="uq_event_slot_name"),
        sa.CheckConstraint(
            "capacity IS NULL OR capacity >= 1", name="ck_event_slot_capacity"
        ),
    )

    op.create_table(
        "event_assignment",
        sa.Column("id", sa.Integer, primary_key=True),
        # index (below) serves the capacity count in sign_up/assign
        sa.Column(
            "slot_id",
            sa.Integer,
            sa.ForeignKey("event_slot.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # denormalized so per-event queries and the one-slot-per-person unique
        # constraint are direct; the service guarantees slot_id ∈ event_id
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # index serves "my upcoming assignments" and the hours summary
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "assigned_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("assigned_notified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("reminder_sent_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("attended_override", sa.Boolean),
        sa.Column("hours_override", sa.Numeric(5, 2)),
        # one slot per person per event; also serves event-scoped reads
        sa.UniqueConstraint("event_id", "volunteer_id", name="uq_event_assignment"),
        sa.CheckConstraint(
            "kind IN ('signup', 'assigned', 'sub')", name="ck_event_assignment_kind"
        ),
        sa.CheckConstraint(
            "hours_override IS NULL OR hours_override >= 0",
            name="ck_event_assignment_hours",
        ),
    )

    op.create_table(
        "event_rsvp",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # index serves "my RSVPs" (the unique below leads with event_id)
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("available", sa.Boolean, nullable=False),
        sa.Column("note", sa.String(200)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("event_id", "volunteer_id", name="uq_event_rsvp"),
    )

    op.create_table(
        "event_sub_request",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Integer,
            sa.ForeignKey("event_assignment.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "requested_by",
            sa.Integer,
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
        ),
        sa.Column("note", sa.String(200)),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column(
            "claimed_by_volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "status IN ('open', 'claimed', 'cancelled')",
            name="ck_event_sub_request_status",
        ),
    )
    # at most one OPEN substitution request per assignment
    op.create_index(
        "uq_event_sub_request_open",
        "event_sub_request",
        ["assignment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_table("event_sub_request")
    op.drop_table("event_rsvp")
    op.drop_table("event_assignment")
    op.drop_table("event_slot")
    op.drop_table("event")
