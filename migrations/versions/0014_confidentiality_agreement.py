"""signup confidentiality notice: app_user.confidentiality_agreed_at

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-16

Account setup (/invite/{token}) now requires agreeing not to disclose
volunteers' personal information without their consent; this column records
when each person accepted. Existing accounts predate the notice and are left
NULL rather than backfilled — the column records actual acceptance, not an
assumption. (Reissuing an invite — the password-reset path — sends the person
back through the notice, which refreshes their timestamp.)

app_user is not system-versioned (no history twin), so plain add_column
suffices — the rebuild recipe in 0002's docstring does not apply here.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column(
            "confidentiality_agreed_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("app_user", "confidentiality_agreed_at")
