"""Event assignments: per-stage reminder preferences and stamps

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-17

The single rolling reminder_sent_at becomes two opt-outable stages: "this
week" (7 days out) and "tomorrow" (1 day out), each with its own pre-checked
preference and its own NULL-stamp idempotency column. Rows already reminded
under the old scheme carry their stamp into reminded_7d_at, so nobody is
re-notified for an event they were already reminded of; their day-before
notice still fires, which is the new deal, not a regression.

event_assignment is not system-versioned (0016) — no history-twin rebuild.
Downgrade collapses both stamps into the old column (earliest wins) and
drops the preferences.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_assignment",
        sa.Column("notify_7d", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "event_assignment",
        sa.Column("notify_24h", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "event_assignment", sa.Column("reminded_7d_at", sa.TIMESTAMP(timezone=True))
    )
    op.add_column(
        "event_assignment", sa.Column("reminded_24h_at", sa.TIMESTAMP(timezone=True))
    )
    op.execute("UPDATE event_assignment SET reminded_7d_at = reminder_sent_at")
    op.drop_column("event_assignment", "reminder_sent_at")


def downgrade() -> None:
    op.add_column(
        "event_assignment",
        sa.Column("reminder_sent_at", sa.TIMESTAMP(timezone=True)),
    )
    op.execute(
        "UPDATE event_assignment"
        " SET reminder_sent_at = LEAST(reminded_7d_at, reminded_24h_at)"
    )
    op.drop_column("event_assignment", "reminded_24h_at")
    op.drop_column("event_assignment", "reminded_7d_at")
    op.drop_column("event_assignment", "notify_24h")
    op.drop_column("event_assignment", "notify_7d")
