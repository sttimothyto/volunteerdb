"""Events: Google Calendar sync bookkeeping

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-17

Two nullable columns on event, owned by jobs/calendar_sync.py: the Google
event id a scheduled event was pushed as, and a fingerprint of the payload
last pushed, so the 30-minute reconcile can tell "changed" from "already
current" without an updated_at column. NULL google_event_id = never pushed;
the first configured run pushes everything upcoming, so there is no
backfill. event is not system-versioned (0016), so no history-twin rebuild.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event", sa.Column("google_event_id", sa.String(1024)))
    op.add_column("event", sa.Column("google_fingerprint", sa.String(64)))


def downgrade() -> None:
    op.drop_column("event", "google_fingerprint")
    op.drop_column("event", "google_event_id")
