"""custom fields + capacity: custom_field_def, app_setting, volunteer.custom, team.workload_weight

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11

NOTE: the versioning() trigger copies rows into <table>_history POSITIONALLY
(INSERT ... SELECT ($1).*, changed_by, op), so a twin must hold the live
columns in live order followed by (changed_by, op). Postgres can only append
columns, which in the twin would land AFTER changed_by/op — so adding a live
column means REBUILDING the twin. Recipe (reuse for future column adds):

  1. op.add_column(<live table>, <new column>)      -- appends after sys_period
  2. CREATE TABLE <t>_history_new (live cols in order incl. new col, changed_by, op)
  3. INSERT INTO <t>_history_new SELECT <old cols>, NULL, changed_by, op FROM <t>_history
  4. DROP TABLE <t>_history; ALTER TABLE <t>_history_new RENAME TO <t>_history
  5. recreate ix_<t>_history_id (btree) and ix_<t>_history_sys_period (gist)

No trigger changes needed: it resolves <table>_history by name at runtime.
Pre-existing history rows get NULL for the new column (the field didn't exist
yet); readers treat NULL custom as {}.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# live-table column order as of rev 0001 (name, SQL type)
VOLUNTEER_COLS = [
    ("id", "integer"),
    ("first_name", "varchar(100)"),
    ("last_name", "varchar(100)"),
    ("email", "varchar(255)"),
    ("phone", "varchar(50)"),
    ("notes", "text"),
    ("is_active", "boolean"),
    ("created_at", "timestamptz"),
    ("updated_at", "timestamptz"),
    ("sys_period", "tstzrange"),
]
TEAM_COLS = [
    ("id", "integer"),
    ("name", "varchar(200)"),
    ("parent_team_id", "integer"),
    ("description", "text"),
    ("is_active", "boolean"),
    ("sys_period", "tstzrange"),
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
    op.create_table(
        "custom_field_def",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(50), nullable=False, unique=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("field_type", sa.String(20), nullable=False),
        sa.Column("options", postgresql.JSONB),
        sa.Column(
            "show_in_list", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("position", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "field_type IN ('text', 'number', 'select', 'date', 'checkbox')",
            name="ck_custom_field_def_field_type",
        ),
    )

    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.add_column(
        "volunteer",
        sa.Column(
            "custom",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    _rebuild_history(
        "volunteer",
        [*VOLUNTEER_COLS, ("custom", "jsonb"), *AUDIT_COLS],
        [*(name for name, _ in VOLUNTEER_COLS), "NULL", "changed_by", "op"],
    )

    op.add_column("team", sa.Column("workload_weight", sa.Numeric(8, 2)))
    _rebuild_history(
        "team",
        [*TEAM_COLS, ("workload_weight", "numeric(8,2)"), *AUDIT_COLS],
        [*(name for name, _ in TEAM_COLS), "NULL", "changed_by", "op"],
    )


def downgrade() -> None:
    _rebuild_history(
        "team",
        [*TEAM_COLS, *AUDIT_COLS],
        [*(name for name, _ in TEAM_COLS), "changed_by", "op"],
    )
    op.drop_column("team", "workload_weight")

    _rebuild_history(
        "volunteer",
        [*VOLUNTEER_COLS, *AUDIT_COLS],
        [*(name for name, _ in VOLUNTEER_COLS), "changed_by", "op"],
    )
    op.drop_column("volunteer", "custom")

    op.drop_table("app_setting")
    op.drop_table("custom_field_def")
