"""A notice records who was told.

``notification.volunteer_id`` names the recipient. For an assignment row that
is the holder at the time: a slot changes hands (claim_sub, substitute) and the
incoming person has had none of the outgoing one's notices. Until now the
services compensated -- deleting the reminder stamps on every hand-over, and in
one path the "scheduled" stamp too -- because a row could say which
assignment but not whom. With the recipient in the key the outgoing person's
stamps simply stop matching, nothing is deleted, and what they were told stays
on record. For a voter row it repeats proposal_voter.volunteer_id, which never
changes; it is there so the column can be NOT NULL and mean one thing.

Backfilled from the current holder, or the voter, which is the best the rows
already there can say. uq_notification_assignment widens to (assignment_id,
volunteer_id, stage); the voter unique is unchanged. The new FK is a cascade
path from volunteer, so it gets an index.

NOT transparent to the running image: its stamp inserts name no recipient and
fail on the NOT NULL between migrate and restart. A claim or a hand-over in
those seconds is refused with a conflict, and a digest that happens to be
running stamps nothing and retries the next night. Deploy at a quiet hour
(docs/how-to/deploy.md).

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE notification ADD COLUMN volunteer_id INTEGER""")
    op.execute(
        """UPDATE notification n SET volunteer_id = a.volunteer_id
	FROM event_assignment a WHERE n.assignment_id = a.id"""
    )
    op.execute(
        """UPDATE notification n SET volunteer_id = v.volunteer_id
	FROM proposal_voter v WHERE n.voter_id = v.id"""
    )
    op.execute("""ALTER TABLE notification ALTER COLUMN volunteer_id SET NOT NULL""")
    op.execute(
        """ALTER TABLE notification ADD CONSTRAINT notification_volunteer_id_fkey
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE"""
    )
    op.execute(
        """CREATE INDEX ix_notification_volunteer_id ON notification (volunteer_id)"""
    )
    op.execute(
        """ALTER TABLE notification DROP CONSTRAINT uq_notification_assignment"""
    )
    op.execute(
        """ALTER TABLE notification ADD CONSTRAINT uq_notification_assignment
	UNIQUE (assignment_id, volunteer_id, stage)"""
    )


def downgrade() -> None:
    """Keeps, per (assignment, stage), only the current holder's row -- the one
    the old code could see -- so the narrower unique can be restored. What a
    previous holder was told is lost with the column."""
    op.execute(
        """DELETE FROM notification n USING event_assignment a
	WHERE n.assignment_id = a.id AND n.volunteer_id <> a.volunteer_id"""
    )
    op.execute(
        """ALTER TABLE notification DROP CONSTRAINT uq_notification_assignment"""
    )
    op.execute(
        """ALTER TABLE notification ADD CONSTRAINT uq_notification_assignment
	UNIQUE (assignment_id, stage)"""
    )
    op.execute("""ALTER TABLE notification DROP COLUMN volunteer_id""")
