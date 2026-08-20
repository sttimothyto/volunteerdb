"""A team's roster spreadsheet is a link its leaders set, not a request an
admin files.

Revision 0003 added requested_file_id/requested_import because the app could
not reach Drive: rclone ran on the host under a drive.file grant, so a pasted
link could only be checked by the next nightly sync, and had to be parked
beside file_id until then in case it turned out to be invisible.

Roster sheets are now shared "anyone with the link can edit", which the app
reads over the anonymous CSV export endpoint and writes with the parish
account's token (services/gsheets.py). A link can therefore be validated the
moment it is pasted, so there is nothing left to hold in a request column:
services.teams.set_roster_sheet writes file_id directly.

team_sheet is not system-versioned (it is a pointer at an external artifact,
current-state only), so there is no history twin to rebuild here.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """ALTER TABLE team_sheet
	DROP COLUMN requested_file_id,
	DROP COLUMN requested_import"""
    )


def downgrade() -> None:
    """Restores the columns empty. Any request pending when 0005 ran is gone,
    which costs nothing: an unadopted request never touched parish data, and
    the link it held is now in file_id anyway if a sync adopted it."""
    op.execute(
        """ALTER TABLE team_sheet
	ADD COLUMN requested_file_id VARCHAR(128) UNIQUE,
	ADD COLUMN requested_import BOOLEAN DEFAULT false NOT NULL"""
    )
