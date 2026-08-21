"""A parish can show its own logo instead of the stock church glyph.

Admins upload one image, and it replaces the ⛪ emoji in the app header, above
the login box, and on the public ministries shell. The bytes live in Postgres
like every other binary here (volunteer_photo, team_page_image) — this app has
no object store, and ui/static is baked into the container image, not writable
at runtime.

One row, pinned by a CHECK: an upload replaces the logo rather than piling up
versions. Not system-versioned, matching the two image tables it sits beside —
branding is current-state only.

Purely additive, so the running image (which never references this table) keeps
serving while the migration applies.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_logo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=50),
            nullable=False,
            server_default="image/png",
        ),
        sa.Column(
            "uploaded_by",
            sa.Integer(),
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "uploaded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="ck_site_logo_singleton"),
    )


def downgrade() -> None:
    op.drop_table("site_logo")
