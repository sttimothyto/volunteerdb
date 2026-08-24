"""Every account may have a personal calendar feed address.

`app_user.calendar_token` is the last path segment of
/calendar/mine/<token>.ics, the reader's own duties as an iCalendar feed
their phone or desktop calendar subscribes to. It is stored in clear, unlike
the account's other three tokens, because it has to be shown again on every
visit to the subscribe panel -- a feed address you cannot see again is one
you cannot subscribe to -- and because what it unlocks is a duty list, not a
sign-in. NULL until the account first asks for one; rotated from the panel.

Purely additive, so the running image keeps serving through the migration.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE app_user ADD COLUMN calendar_token VARCHAR(64)""")
    op.execute(
        """ALTER TABLE app_user ADD CONSTRAINT app_user_calendar_token_key
           UNIQUE (calendar_token)"""
    )


def downgrade() -> None:
    """Every subscribed feed goes dark; nothing else changes."""
    op.execute("""ALTER TABLE app_user DROP COLUMN calendar_token""")
