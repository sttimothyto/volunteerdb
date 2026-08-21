"""Count the mail this instance sends, and stop pre-checking the 7-day reminder.

Two changes, one purpose: staying inside the mail provider's free tier (200
messages a day, 1,000 a month), which the app otherwise discovers it has left
only by a sign-in code failing to arrive.

`mail_quota` is a counter, one row per parish day, upserted by the single
success path in `services/mail.py`. Deliberately not a log — no address, no
subject — so it can never become a second copy of who was mailed what.
`services/mail_quota.py` reads it into the admin-only banner.

`event_assignment.notify_7d` loses its `true` default. The 7-day reminder
restates a "you have been scheduled" notice the volunteer already had, and the
24-hour one is what actually changes their day; on a weekend roster it was a
third of all event mail. The column, the preference and the digest stage all
stay — only the default flips, so a volunteer or manager can still tick it per
assignment. Existing rows keep whatever was chosen for them: a recorded
preference is not ours to rewrite.

Purely additive plus a default change, so the running image keeps serving
through the migration — nothing it maps is dropped or renamed.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_quota",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("sent", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.alter_column(
        "event_assignment",
        "notify_7d",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.false(),
    )


def downgrade() -> None:
    op.alter_column(
        "event_assignment",
        "notify_7d",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
    )
    op.drop_table("mail_quota")
