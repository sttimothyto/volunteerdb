"""An admin may repoint a team at a different roster spreadsheet.

The request is held BESIDE team_sheet.file_id rather than over it. Nothing in
the app can reach Drive -- the sync is rclone on the host, under a drive.file
grant that sees only files this system created -- so a pasted link is only
checked by the next nightly run. Storing it separately means a link that turns
out to be invisible or malformed costs the team nothing: it keeps the sheet it
already had, and the rejection is reported on the team page.

team_sheet is not system-versioned (it is a pointer at an external artifact,
current-state only), so there is no history twin to rebuild here.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # UNIQUE for the same reason file_id is: two teams pointed at one sheet
    # would each rewrite the other's roster every night.
    op.execute(
        """ALTER TABLE team_sheet
	ADD COLUMN requested_file_id VARCHAR(128) UNIQUE,
	ADD COLUMN requested_import BOOLEAN DEFAULT false NOT NULL"""
    )


def downgrade() -> None:
    """Drops the columns and any request still pending in them -- an unadopted
    request has no effect on parish data, so there is nothing to preserve."""
    op.execute(
        """ALTER TABLE team_sheet
	DROP COLUMN requested_file_id,
	DROP COLUMN requested_import"""
    )
