"""Nomination + STAR-voting planning pipeline.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-02

Replaces the single-candidate proposal flow shipped in 0007 (never used in
production; any rows are dropped) with a pipeline: a proposal now targets
one (team, role) seat and carries its own candidates, voting roll and two
deadlines. Candidates are nominated until the nomination deadline, the roll
scores them 0-5 (STAR) until the voting deadline, then a manager appoints.
The membership an appointment creates *is* versioned; the pipeline tables
are not — workflow data whose lifecycle is self-recorded, like 0007's
table. Ballot scores are secret: the score column is listed in
audit.REDACTED_COLUMNS so values never reach a log line.

New non-versioned tables only: no versioned-table columns move, so the
history-twin positional-parity rebuild recipe from revision 0002 does not
apply.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# the type is owned by revision 0001; create_type=False just references it
team_role = postgresql.ENUM(
    "leader", "second", "core", "member", name="team_role", create_type=False
)


def upgrade() -> None:
    op.drop_index("uq_proposal_open", table_name="proposal")
    op.drop_table("proposal")

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
        sa.Column("role", team_role, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("notes", sa.Text),
        sa.Column("nomination_deadline", sa.Date, nullable=False),
        sa.Column("voting_deadline", sa.Date, nullable=False),
        # FK to proposal_candidate is circular; added below once both exist
        sa.Column("appointed_candidate_id", sa.Integer),
        sa.Column(
            "created_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "decided_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "status IN ('open', 'appointed', 'cancelled')",
            name="ck_proposal_status",
        ),
        sa.CheckConstraint(
            "nomination_deadline < voting_deadline",
            name="ck_proposal_deadlines",
        ),
    )
    op.create_table(
        "proposal_candidate",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "proposal_id",
            sa.Integer,
            sa.ForeignKey("proposal.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("note", sa.Text),
        sa.Column(
            "nominated_by",
            sa.Integer,
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "proposal_id", "volunteer_id", name="uq_proposal_candidate"
        ),
    )
    op.create_foreign_key(
        "fk_proposal_appointed_candidate",
        "proposal",
        "proposal_candidate",
        ["appointed_candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "proposal_voter",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "proposal_id",
            sa.Integer,
            sa.ForeignKey("proposal.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # index serves load_actor's "which rolls am I on?" per-request lookup
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "added_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("proposal_id", "volunteer_id", name="uq_proposal_voter"),
    )
    op.create_table(
        "proposal_ballot",
        sa.Column("id", sa.Integer, primary_key=True),
        # denormalized so tally/turnout are one query; the service guarantees
        # the voter and candidate rows belong to this proposal
        sa.Column(
            "proposal_id",
            sa.Integer,
            sa.ForeignKey("proposal.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "voter_id",
            sa.Integer,
            sa.ForeignKey("proposal_voter.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Integer,
            sa.ForeignKey("proposal_candidate.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("score", sa.SmallInteger, nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("voter_id", "candidate_id", name="uq_proposal_ballot"),
        sa.CheckConstraint("score BETWEEN 0 AND 5", name="ck_proposal_ballot_score"),
    )
    # at most one OPEN proposal per seat; decided ones stay as history and
    # opening a fresh round for the same seat remains legal
    op.create_index(
        "uq_proposal_open",
        "proposal",
        ["team_id", "role"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_table("proposal_ballot")
    op.drop_table("proposal_voter")
    op.drop_constraint(
        "fk_proposal_appointed_candidate", "proposal", type_="foreignkey"
    )
    op.drop_table("proposal_candidate")
    op.drop_index("uq_proposal_open", table_name="proposal")
    op.drop_table("proposal")

    # restore 0007's single-candidate proposal table verbatim
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
        sa.Column(
            "proposed_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "decided_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'declined', 'withdrawn')",
            name="ck_proposal_status",
        ),
    )
    op.create_index(
        "uq_proposal_open",
        "proposal",
        ["team_id", "role", "volunteer_id"],
        unique=True,
        postgresql_where=sa.text("status = 'proposed'"),
    )
    # team_role enum stays: owned by revision 0001
