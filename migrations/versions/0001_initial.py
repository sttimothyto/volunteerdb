"""initial schema: core tables, history twins, versioning triggers

Revision ID: 0001
Revises:
Create Date: 2026-07-11

NOTE: volunteer/team/membership are system-versioned via the versioning()
trigger. Any future ALTER on those tables must be mirrored on the matching
<table>_history twin, or the trigger's INSERT ... SELECT ($1).* will break.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

team_role = postgresql.ENUM(
    "leader", "second", "core", "member", name="team_role", create_type=False
)

SYS_PERIOD_DEFAULT = sa.text("tstzrange(clock_timestamp(), NULL)")

VERSIONED_TABLES = ("volunteer", "team", "membership")

VERSIONING_FN = """
CREATE OR REPLACE FUNCTION versioning() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    hist regclass := (quote_ident(TG_TABLE_SCHEMA) || '.'
                      || quote_ident(TG_TABLE_NAME || '_history'))::regclass;
    uid integer := NULLIF(current_setting('app.user_id', true), '')::integer;
    ts timestamptz := clock_timestamp();
BEGIN
    IF TG_OP = 'UPDATE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format('INSERT INTO %s SELECT ($1).*, $2, $3', hist) USING OLD, uid, 'U';
        NEW.sys_period := tstzrange(ts, NULL);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format('INSERT INTO %s SELECT ($1).*, $2, $3', hist) USING OLD, uid, 'D';
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;
"""


def _history_columns(live_columns: list[sa.Column]) -> list[sa.schema.SchemaItem]:
    return [
        *live_columns,
        sa.Column("changed_by", sa.Integer),
        sa.Column("op", sa.CHAR(1)),
    ]


def upgrade() -> None:
    op.execute("CREATE TYPE team_role AS ENUM ('leader', 'second', 'core', 'member')")

    op.create_table(
        "volunteer",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("notes", sa.Text),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
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
        sa.Column(
            "sys_period",
            postgresql.TSTZRANGE,
            nullable=False,
            server_default=SYS_PERIOD_DEFAULT,
        ),
    )
    op.create_index("ix_volunteer_email", "volunteer", ["email"])

    op.create_table(
        "team",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "parent_team_id", sa.Integer, sa.ForeignKey("team.id", ondelete="RESTRICT")
        ),
        sa.Column("description", sa.Text),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "sys_period",
            postgresql.TSTZRANGE,
            nullable=False,
            server_default=SYS_PERIOD_DEFAULT,
        ),
        sa.UniqueConstraint(
            "parent_team_id", "name", postgresql_nulls_not_distinct=True
        ),
    )

    op.create_table(
        "membership",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", team_role, nullable=False),
        sa.Column("joined_on", sa.Date),
        sa.Column("notes", sa.Text),
        sa.Column(
            "sys_period",
            postgresql.TSTZRANGE,
            nullable=False,
            server_default=SYS_PERIOD_DEFAULT,
        ),
        sa.UniqueConstraint("volunteer_id", "team_id"),
    )
    op.create_index("ix_membership_volunteer_id", "membership", ["volunteer_id"])
    op.create_index("ix_membership_team_id", "membership", ["team_id"])

    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("api_token", sa.String(64), unique=True),
        sa.Column("invite_token", sa.String(64), unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- history twins: same column order as live tables + (changed_by, op) ---
    op.create_table(
        "volunteer_history",
        *_history_columns(
            [
                sa.Column("id", sa.Integer),
                sa.Column("first_name", sa.String(100)),
                sa.Column("last_name", sa.String(100)),
                sa.Column("email", sa.String(255)),
                sa.Column("phone", sa.String(50)),
                sa.Column("notes", sa.Text),
                sa.Column("is_active", sa.Boolean),
                sa.Column("created_at", sa.TIMESTAMP(timezone=True)),
                sa.Column("updated_at", sa.TIMESTAMP(timezone=True)),
                sa.Column("sys_period", postgresql.TSTZRANGE),
            ]
        ),
    )
    op.create_table(
        "team_history",
        *_history_columns(
            [
                sa.Column("id", sa.Integer),
                sa.Column("name", sa.String(200)),
                sa.Column("parent_team_id", sa.Integer),
                sa.Column("description", sa.Text),
                sa.Column("is_active", sa.Boolean),
                sa.Column("sys_period", postgresql.TSTZRANGE),
            ]
        ),
    )
    op.create_table(
        "membership_history",
        *_history_columns(
            [
                sa.Column("id", sa.Integer),
                sa.Column("volunteer_id", sa.Integer),
                sa.Column("team_id", sa.Integer),
                sa.Column("role", team_role),
                sa.Column("joined_on", sa.Date),
                sa.Column("notes", sa.Text),
                sa.Column("sys_period", postgresql.TSTZRANGE),
            ]
        ),
    )
    for table in VERSIONED_TABLES:
        op.create_index(f"ix_{table}_history_id", f"{table}_history", ["id"])
        op.create_index(
            f"ix_{table}_history_sys_period",
            f"{table}_history",
            ["sys_period"],
            postgresql_using="gist",
        )

    op.execute(VERSIONING_FN)
    for table in VERSIONED_TABLES:
        op.execute(
            f"CREATE TRIGGER versioning_trigger BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION versioning()"
        )


def downgrade() -> None:
    for table in VERSIONED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS versioning_trigger ON {table}")
    op.execute("DROP FUNCTION IF EXISTS versioning()")
    for table in (
        "membership_history",
        "team_history",
        "volunteer_history",
        "app_user",
        "membership",
        "team",
        "volunteer",
    ):
        op.drop_table(table)
    op.execute("DROP TYPE team_role")
