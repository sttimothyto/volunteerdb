"""interest pipeline + proposal digest stamps: team.application_form_url,
interest, proposal_voter.*_notified_at

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-16

team.application_form_url is a new live column on a versioned table, so the
team_history twin is rebuilt (recipe and rationale in 0002's docstring).
It holds the team's own Google application form, mailed to people who express
interest on the public ministry page.

interest records one outsider's "I'm interested" submission from the public
/ministries page. Not system-versioned (like proposal): workflow data whose
lifecycle is self-recorded in created_at/resolved_at. The partial unique
index allows at most one OPEN interest per (team, lowercased email) — the
dedup that keeps repeat submissions from re-mailing leaders and applicants.

proposal_voter gains two nullable notification stamps read and set only by
jobs.proposal_digest: when the nightly digest told this voter they were added
to the roll, and when it told them voting began. Per-voter (not per-proposal)
so a failed send retries just that person the next night.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# live-table column order as of rev 0010 (name, SQL type)
TEAM_COLS = [
    ("id", "integer"),
    ("name", "varchar(200)"),
    ("parent_team_id", "integer"),
    ("description", "text"),
    ("is_active", "boolean"),
    ("sys_period", "tstzrange"),
    ("workload_weight", "numeric(8,2)"),
    ("home_doc_url", "varchar(500)"),
]
AUDIT_COLS = [("changed_by", "integer"), ("op", "char(1)")]


def _rebuild_history(
    table: str, cols: list[tuple[str, str]], select_exprs: list[str]
) -> None:
    """Recreate <table>_history with `cols`, carrying rows over via `select_exprs`."""
    hist = f"{table}_history"
    ddl = ", ".join(f"{name} {sql_type}" for name, sql_type in cols)
    op.execute(f"CREATE TABLE {hist}_new ({ddl})")
    op.execute(f"INSERT INTO {hist}_new SELECT {', '.join(select_exprs)} FROM {hist}")
    op.execute(f"DROP TABLE {hist}")
    op.execute(f"ALTER TABLE {hist}_new RENAME TO {hist}")
    op.create_index(f"ix_{hist}_id", hist, ["id"])
    op.create_index(
        f"ix_{hist}_sys_period", hist, ["sys_period"], postgresql_using="gist"
    )


def upgrade() -> None:
    op.add_column("team", sa.Column("application_form_url", sa.String(500)))
    _rebuild_history(
        "team",
        [*TEAM_COLS, ("application_form_url", "varchar(500)"), *AUDIT_COLS],
        [*(name for name, _ in TEAM_COLS), "NULL", "changed_by", "op"],
    )

    op.create_table(
        "interest",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),  # stored lowercased
        sa.Column("phone", sa.String(50)),
        sa.Column("note", sa.Text),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "resolved_by",
            sa.Integer,
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("ix_interest_team_id", "interest", ["team_id"])
    op.create_index(
        "uq_interest_open",
        "interest",
        ["team_id", "email"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    op.add_column(
        "proposal_voter",
        sa.Column("added_notified_at", sa.TIMESTAMP(timezone=True)),
    )
    op.add_column(
        "proposal_voter",
        sa.Column("voting_notified_at", sa.TIMESTAMP(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("proposal_voter", "voting_notified_at")
    op.drop_column("proposal_voter", "added_notified_at")

    op.drop_table("interest")

    _rebuild_history(
        "team",
        [*TEAM_COLS, *AUDIT_COLS],
        [*(name for name, _ in TEAM_COLS), "changed_by", "op"],
    )
    op.drop_column("team", "application_form_url")
