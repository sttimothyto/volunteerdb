"""A slot may carry a line of its own beside its name.

"Greeter" is the name; "main door, from 10:00" is the detail. They were the
same field until now, which pushed prose into the identifier the weekly
copy-forward matches slots on -- rename the slot to explain it and every later
week silently stops matching. Splitting them keeps the name short on purpose.

event_slot is not system-versioned, so this is a plain column add with no
history twin to rebuild.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE event_slot ADD COLUMN description VARCHAR(300)""")


def downgrade() -> None:
    """Drops the notes with the column. They are decoration on a slot that
    keeps its name, its capacity and its roster, so nothing else is affected."""
    op.execute("""ALTER TABLE event_slot DROP COLUMN description""")
