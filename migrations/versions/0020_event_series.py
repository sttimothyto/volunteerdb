"""Events: a shared series id across weekly repeats

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-17

Nullable UUID stamped by create_event on every row a repeat-weekly-until
materializes. Still no recurrence engine — the id exists solely so a
sign-up can copy itself onto the later weeks of the same series. No
backfill: pre-existing repeats stay independent rows (nothing recorded
which creates they came from), so the repeat-sign-up offer simply does not
appear on them. event is not system-versioned (0016).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event", sa.Column("series_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    # partial: most events are standalone and the sweep is always "future
    # rows OF this series"
    op.create_index(
        "ix_event_series",
        "event",
        ["series_id"],
        postgresql_where=sa.text("series_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_event_series", "event")
    op.drop_column("event", "series_id")
