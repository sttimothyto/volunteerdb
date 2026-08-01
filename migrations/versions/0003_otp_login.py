"""email OTP login: app_user otp_hash/otp_sent_at/otp_expires_at/otp_attempts

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-19

app_user is not system-versioned (no history twin), so plain add_column
suffices — the rebuild recipe in 0002's docstring does not apply here.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("otp_hash", sa.String(255), nullable=True))
    op.add_column(
        "app_user", sa.Column("otp_sent_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "app_user",
        sa.Column("otp_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "app_user",
        sa.Column(
            "otp_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade() -> None:
    for col in ("otp_attempts", "otp_expires_at", "otp_sent_at", "otp_hash"):
        op.drop_column("app_user", col)
