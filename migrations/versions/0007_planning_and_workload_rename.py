"""Planning proposals table; app_setting key capacity -> workload.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-01

The Planning page lets admins and team leaders propose volunteers for vacant
roles; a proposal is accepted (creating the membership), declined, or
withdrawn. Deliberately not system-versioned — workflow data whose lifecycle
is already self-recorded in status/created_at/decided_at/decided_by, like
custom_field_def — the accepted membership itself *is* versioned.

Also renames the app_setting key "capacity" to "workload" to match the
UI-wide capacity -> workload rename shipped with this revision.

New non-versioned table only: no versioned-table columns move, so the
history-twin positional-parity rebuild recipe from revision 0002 does not
apply.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# the type is owned by revision 0001; create_type=False just references it
team_role = postgresql.ENUM("leader", "second", "core", "member", name="team_role", create_type=False)


def upgrade() -> None:
    op.create_table(
        "proposal",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", team_role, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("note", sa.Text),
        sa.Column("proposed_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("decided_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'declined', 'withdrawn')",
            name="ck_proposal_status",
        ),
    )
    # at most one OPEN proposal per (team, role, volunteer); resolved ones stay
    # as history and re-proposing after a decline remains legal
    op.create_index(
        "uq_proposal_open",
        "proposal",
        ["team_id", "role", "volunteer_id"],
        unique=True,
        postgresql_where=sa.text("status = 'proposed'"),
    )
    op.execute("UPDATE app_setting SET key = 'workload' WHERE key = 'capacity'")


def downgrade() -> None:
    op.execute("UPDATE app_setting SET key = 'capacity' WHERE key = 'workload'")
    op.drop_index("uq_proposal_open", table_name="proposal")
    op.drop_table("proposal")
    # team_role enum stays: owned by revision 0001
