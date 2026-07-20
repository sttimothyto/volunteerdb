"""Store only a SHA-256 digest of API tokens; invalidate existing plaintext ones.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-19

Tokens were previously stored in plaintext. Lookups now compare digests, so
existing rows can never match again — null them; users get a fresh token on
their next login.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE app_user SET api_token = NULL")


def downgrade() -> None:
    pass  # digests cannot be un-hashed; nothing to restore
