"""Drop membership.joined_on and membership.notes; rebuild the history twin.

Revision ID: 0011
Revises: 0010

The roster slimmed to ID/name/contact/Team/Role (rev 5165d0b): join dates
and per-membership notes had no reader left outside the CSV pipeline, so
the columns go entirely. Timeline spell starts now derive from sys_period
alone.

DATA LOSS (deliberate): joined_on and notes values are discarded from the
live table AND from every membership_history version. downgrade() restores
the columns but their values come back NULL — recover real values only from
a pre-0011 pg_dump or the archived ten-column parish export taken before
deploy.

Twin-rebuild recipe as in 0002/0010 — the versioning() trigger archives
positionally (INSERT INTO <t>_history SELECT ($1).*, ...), and PostgreSQL
skips dropped columns when expanding a row, so the twin must be rebuilt to
the post-drop live order. One extra step the 0010 helper does not cover:
membership_history carries a third index (ix_membership_history_volunteer_id,
rev 0005) that must be recreated after the rebuild.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# live-table column order AFTER the drops (name, SQL type)
MEMBERSHIP_COLS = [
    ("id", "integer"),
    ("volunteer_id", "integer"),
    ("team_id", "integer"),
    ("role", "team_role"),
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
    op.drop_column("membership", "joined_on")
    op.drop_column("membership", "notes")
    _rebuild_history(
        "membership",
        [*MEMBERSHIP_COLS, *AUDIT_COLS],
        # joined_on/notes history values are dropped here, deliberately
        [*(name for name, _ in MEMBERSHIP_COLS), "changed_by", "op"],
    )
    op.create_index(
        "ix_membership_history_volunteer_id", "membership_history", ["volunteer_id"]
    )


def downgrade() -> None:
    # add_column appends after sys_period, so the restored live order differs
    # from the 0001 original; the twin below mirrors the POST-downgrade order.
    # Values are NULL — the data is gone (see module docstring).
    op.execute("ALTER TABLE membership ADD COLUMN joined_on date")
    op.execute("ALTER TABLE membership ADD COLUMN notes text")
    _rebuild_history(
        "membership",
        [*MEMBERSHIP_COLS, ("joined_on", "date"), ("notes", "text"), *AUDIT_COLS],
        [*(name for name, _ in MEMBERSHIP_COLS), "NULL", "NULL", "changed_by", "op"],
    )
    op.create_index(
        "ix_membership_history_volunteer_id", "membership_history", ["volunteer_id"]
    )
