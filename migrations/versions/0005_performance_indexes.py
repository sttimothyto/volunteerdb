"""Performance indexes: trigram search on volunteer, membership_history FK.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-22

The volunteer search box filters with a leading-wildcard ILIKE over
(first_name || ' ' || last_name) and email, which no btree can serve —
pg_trgm GIN indexes flip it from a sequential scan to a bitmap scan once
the table is big enough to matter (measured: in-database cost -85% at
5000 volunteers; at 500 the planner rightly sticks with the seq scan).
The timeline view filters membership_history by volunteer_id, previously
a full scan of a table that grows without bound. The (last_name,
first_name) btree serves every list's ORDER BY with an ordered index
scan instead of seq-scan-and-sort.

Index-only change: no versioned-table columns move, so the history-twin
positional-parity rebuild recipe from revision 0002 does not apply.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pg_trgm is a trusted extension (installable without superuser on PG13+)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # expression matches what SQLAlchemy renders for services.volunteers.search
    op.execute(
        "CREATE INDEX ix_volunteer_full_name_trgm ON volunteer"
        " USING gin ((first_name || ' ' || last_name) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_volunteer_email_trgm ON volunteer USING gin (email gin_trgm_ops)"
    )
    op.create_index(
        "ix_membership_history_volunteer_id", "membership_history", ["volunteer_id"]
    )
    op.create_index("ix_volunteer_name", "volunteer", ["last_name", "first_name"])


def downgrade() -> None:
    op.drop_index("ix_volunteer_name", table_name="volunteer")
    op.drop_index("ix_membership_history_volunteer_id", table_name="membership_history")
    op.execute("DROP INDEX ix_volunteer_email_trgm")
    op.execute("DROP INDEX ix_volunteer_full_name_trgm")
    # pg_trgm stays installed: trusted and inert, and other objects may come to rely on it
