"""Volunteer headshots: volunteer_photo table.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-31

One row per volunteer holding the normalized 400x400 JPEG (at most
services.photos.PHOTO_MAX_BYTES, sized so its base64 always fits an Excel
cell). Deliberately not system-versioned — photos are current-state only,
like custom_field_def — so as-of views show the current photo.

New non-versioned table only: no versioned-table columns move, so the
history-twin positional-parity rebuild recipe from revision 0002 does not
apply.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "volunteer_photo",
        sa.Column(
            "volunteer_id",
            sa.Integer,
            sa.ForeignKey("volunteer.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("image", sa.LargeBinary, nullable=False),
        sa.Column("content_type", sa.String(50), nullable=False, server_default="image/jpeg"),
        sa.Column("uploaded_by", sa.Integer, sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column(
            "uploaded_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("volunteer_photo")
