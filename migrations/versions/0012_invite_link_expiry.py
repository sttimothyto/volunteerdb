"""invite/reset links expire: app_user.invite_expires_at

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-14

The invite link doubles as the password-reset link, so it is a recovery
credential sitting in a mailbox — and until now it sat there forever. NIST SP
800-63B §4.2.1.2 caps a recovery code sent to an email address at 24 hours;
VDB_INVITE_TTL_HOURS carries that default.

Outstanding invites are backfilled to now + 24 h rather than being killed
outright: a volunteer who was emailed a link this morning keeps a usable day
of it, and anyone whose window has closed can still sign in with an emailed
code and set a password from /account.

app_user is not system-versioned (no history twin), so plain add_column
suffices — the rebuild recipe in 0002's docstring does not apply here.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("invite_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE app_user SET invite_expires_at = now() + interval '24 hours' "
        "WHERE invite_token IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("app_user", "invite_expires_at")
