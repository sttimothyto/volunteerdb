"""Nightly-job bookkeeping: job_run

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-16

One row per in-app scheduled job (volunteerdb.scheduler). The table exists
solely so an app restart or redeploy cannot skip a night: the scheduler runs
any job whose last_success_on predates the parish today once its configured
time has passed. last_attempt_at / last_exit_code are diagnostics — "did last
night run, and how did it go" — without journal spelunking.

Not system-versioned (like the 0016 scheduling tables): scheduler
bookkeeping, not parish data; the audit listeners log its writes like any
other.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_run",
        sa.Column("job_name", sa.String(100), primary_key=True),
        sa.Column("last_success_on", sa.Date),  # parish date, not UTC
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_exit_code", sa.Integer),
    )


def downgrade() -> None:
    op.drop_table("job_run")
