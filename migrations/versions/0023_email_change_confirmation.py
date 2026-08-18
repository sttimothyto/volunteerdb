"""a new address proves itself first: app_user email-change columns

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-17

Changing the address you sign in with used to be impossible (no service, no
API field, no form); changing the address on your volunteer record was a plain
edit anyone with rights could make, including you, to an address nobody had
checked. A typo therefore cost a volunteer every notice the app sends, and the
sign-in code with it — the address IS the credential on the emailed-code path,
so an unverified change is an account handed to whoever owns the typo.

So the change is staged here until the *new* address opens a link: pending_email
holds it, email_change_token/email_change_expires_at arm the link. Set and
cleared as a triple, exactly like invite_token/invite_expires_at (0012), so a
token with no live expiry is simply dead. 24 hours, the ceiling NIST SP 800-63B
§4.2.1.2 puts on a code sent to an email address — and unlike the invite link
this one has nothing to trade off against it, because the account keeps working
at the old address for the whole window.

No unique index on pending_email: two people may *ask* for the same address,
and only the first to confirm gets it (app_user.email's own unique constraint,
re-checked at confirm time, settles the race). email_change_token is unique
because it is looked up on its own.

app_user is not system-versioned (no history twin), so plain add_column
suffices — the rebuild recipe in 0002's docstring does not apply here.
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user", sa.Column("pending_email", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "app_user",
        sa.Column("email_change_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "app_user",
        sa.Column(
            "email_change_expires_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
    )
    op.create_unique_constraint(
        "uq_app_user_email_change_token", "app_user", ["email_change_token"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_app_user_email_change_token", "app_user", type_="unique")
    op.drop_column("app_user", "email_change_expires_at")
    op.drop_column("app_user", "email_change_token")
    op.drop_column("app_user", "pending_email")
