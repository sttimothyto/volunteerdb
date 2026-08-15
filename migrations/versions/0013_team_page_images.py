"""team page images: locally cached copies of doc-embedded images

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-14

The Google Doc export hotlinks signed googleusercontent.com URLs that
expire; fetch_and_store now downloads each embedded image and rewrites the
cached html to serve them from /ministries/img/. A current-state-only cache
of external content, deliberately NOT system-versioned — like team_page.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_page_image",
        sa.Column(
            "team_id",
            sa.Integer,
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer, primary_key=True),
        sa.Column("image", sa.LargeBinary, nullable=False),
        sa.Column("content_type", sa.String(50), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("team_page_image")
