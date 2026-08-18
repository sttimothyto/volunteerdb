"""Drop volunteer.updated_at, make workload_weight NOT NULL, natural PK on the
task-force sources, and hold the lowercase-email invariants in the database.

Revision ID: 0024
Revises: 0023

Each of these was a state the code maintained by convention while the schema
allowed the other one:

- **volunteer.updated_at** was redundant with `lower(sys_period)`, which the
  versioning trigger maintains on every write. The second copy was ORM-only, so
  any Core UPDATE left it stale — and nothing read it. DATA LOSS is nominal: the
  value it held is recoverable as `lower(sys_period)`, which is what it meant.
- **team.workload_weight** was nullable, with NULL documented as "counts as 0"
  and coalesced to 0 at every read. A third state with no distinct behaviour;
  existing NULLs become 0, which is what they already scored as.
- **event_task_force_source** had a surrogate `id` beside the `(event_id,
  team_id)` unique that is the row's actual identity. Its only reader was an
  existence probe.
- **interest.email / app_user.email / volunteer.email** were lowercased by the
  services and stored in raw varchar, so one mixed-case row written any other
  way would defeat `uq_interest_open` and the `app_user.email` unique. The
  CHECKs make the convention an invariant; the backfill folds any row that
  already drifted.

Twin-rebuild recipe as in 0002/0010/0011: `versioning()` archives positionally
(`INSERT INTO <t>_history SELECT ($1).*, …`) and PostgreSQL skips dropped
columns when expanding a row, so volunteer_history must be rebuilt to the
post-drop live order.
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# live column order AFTER dropping updated_at (name, SQL type)
VOLUNTEER_COLS = [
    ("id", "integer"),
    ("first_name", "varchar(100)"),
    ("last_name", "varchar(100)"),
    ("email", "varchar(255)"),
    ("phone", "varchar(50)"),
    ("notes", "text"),
    ("is_active", "boolean"),
    ("created_at", "timestamptz"),
    ("sys_period", "tstzrange"),
    ("custom", "jsonb"),
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
    # --- volunteer.updated_at ------------------------------------------------
    op.drop_column("volunteer", "updated_at")
    _rebuild_history(
        "volunteer",
        [*VOLUNTEER_COLS, *AUDIT_COLS],
        [*(name for name, _ in VOLUNTEER_COLS), "changed_by", "op"],
    )
    # the rev-0005 trigram indexes are on the live table only, so they survive

    # --- team.workload_weight NOT NULL DEFAULT 0 -----------------------------
    op.execute("UPDATE team SET workload_weight = 0 WHERE workload_weight IS NULL")
    op.execute(
        "ALTER TABLE team ALTER COLUMN workload_weight SET DEFAULT 0, "
        "ALTER COLUMN workload_weight SET NOT NULL"
    )
    # team_history mirrors types, not nullability or defaults — no rebuild

    # --- event_task_force_source: natural primary key ------------------------
    op.execute(
        "ALTER TABLE event_task_force_source "
        "DROP CONSTRAINT event_task_force_source_pkey"
    )
    op.drop_column("event_task_force_source", "id")
    op.execute(
        "ALTER TABLE event_task_force_source DROP CONSTRAINT uq_task_force_source"
    )
    op.execute(
        "ALTER TABLE event_task_force_source "
        "ADD CONSTRAINT event_task_force_source_pkey PRIMARY KEY (event_id, team_id)"
    )

    # --- lowercase email, enforced -------------------------------------------
    for table in ("interest", "app_user"):
        op.execute(f"UPDATE {table} SET email = lower(email)")
    op.execute("UPDATE volunteer SET email = lower(email) WHERE email <> lower(email)")
    op.execute(
        "ALTER TABLE interest ADD CONSTRAINT ck_interest_email_lower "
        "CHECK (email = lower(email))"
    )
    op.execute(
        "ALTER TABLE app_user ADD CONSTRAINT ck_app_user_email_lower "
        "CHECK (email = lower(email))"
    )
    op.execute(
        "ALTER TABLE volunteer ADD CONSTRAINT ck_volunteer_email_lower "
        "CHECK (email IS NULL OR email = lower(email))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE volunteer DROP CONSTRAINT ck_volunteer_email_lower")
    op.execute("ALTER TABLE app_user DROP CONSTRAINT ck_app_user_email_lower")
    op.execute("ALTER TABLE interest DROP CONSTRAINT ck_interest_email_lower")

    op.execute(
        "ALTER TABLE event_task_force_source "
        "DROP CONSTRAINT event_task_force_source_pkey"
    )
    op.execute("ALTER TABLE event_task_force_source ADD COLUMN id serial PRIMARY KEY")
    op.execute(
        "ALTER TABLE event_task_force_source "
        "ADD CONSTRAINT uq_task_force_source UNIQUE (event_id, team_id)"
    )

    op.execute(
        "ALTER TABLE team ALTER COLUMN workload_weight DROP NOT NULL, "
        "ALTER COLUMN workload_weight DROP DEFAULT"
    )

    # updated_at comes back stamped now(), the way 0011's dropped columns come
    # back NULL. The real values are not lost — they are `lower(sys_period)`,
    # which is what the column meant all along — so anyone who wants them runs
    #   UPDATE volunteer SET updated_at = lower(sys_period);
    # afterwards. It is not done here because assigning a range bound in this
    # position trips a parser error on the asyncpg path.
    op.execute(
        "ALTER TABLE volunteer ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
    )
    _rebuild_history(
        "volunteer",
        [*VOLUNTEER_COLS, ("updated_at", "timestamptz"), *AUDIT_COLS],
        [*(name for name, _ in VOLUNTEER_COLS), "NULL", "changed_by", "op"],
    )
