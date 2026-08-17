"""Events: task-force provenance for team collaboration

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-17

Two provenance tables behind services/task_force.py, which automates the
documented task-force pattern (one event, one team): event_task_force marks
an event whose owning team was swapped for an auto-created "task force"
team so several teams can staff it, remembering who really owns it;
event_task_force_source lists the teams whose rosters were copied in.

Neither is system-versioned (0016 precedent) — the meta team and its
memberships are ordinary team/membership rows, which ARE versioned, and
that is what keeps a torn-down task force visible in history (as-of views).
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_task_force",
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("event.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # the auto-created meta team the event was repointed to
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        # the real owner, restored at teardown BEFORE the meta team dies —
        # event.team_id cascades on team delete, so the order is load-bearing
        sa.Column(
            "owner_team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.Integer,
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "event_task_force_source",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("event_task_force.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("event_id", "team_id", name="uq_task_force_source"),
    )


def downgrade() -> None:
    op.drop_table("event_task_force_source")
    op.drop_table("event_task_force")
