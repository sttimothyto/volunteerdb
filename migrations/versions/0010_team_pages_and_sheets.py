"""team home pages + drive sheets: team.home_doc_url, team_page, team_sheet

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-06

team.home_doc_url is a new live column on a versioned table, so the
team_history twin is rebuilt (recipe and rationale in 0002's docstring).

team_page caches the sanitized HTML fetched nightly from each team's public
Google Doc; team_sheet records the identity of each team's roster sheet in
Google Drive (the stable file id is what keeps leader-facing links working
across team renames). Both are current-state-only caches of external
artifacts, deliberately NOT system-versioned — like volunteer_photo.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# live-table column order as of rev 0002 (name, SQL type)
TEAM_COLS = [
    ("id", "integer"),
    ("name", "varchar(200)"),
    ("parent_team_id", "integer"),
    ("description", "text"),
    ("is_active", "boolean"),
    ("sys_period", "tstzrange"),
    ("workload_weight", "numeric(8,2)"),
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
    op.add_column("team", sa.Column("home_doc_url", sa.String(500)))
    _rebuild_history(
        "team",
        [*TEAM_COLS, ("home_doc_url", "varchar(500)"), *AUDIT_COLS],
        [*(name for name, _ in TEAM_COLS), "NULL", "changed_by", "op"],
    )

    op.create_table(
        "team_page",
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("html", sa.Text),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text),
    )

    op.create_table(
        "team_sheet",
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("file_id", sa.String(128), unique=True),
        sa.Column("file_name", sa.String(300)),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_status", sa.String(20)),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("team_sheet")
    op.drop_table("team_page")

    _rebuild_history(
        "team",
        [*TEAM_COLS, *AUDIT_COLS],
        [*(name for name, _ in TEAM_COLS), "changed_by", "op"],
    )
    op.drop_column("team", "home_doc_url")
