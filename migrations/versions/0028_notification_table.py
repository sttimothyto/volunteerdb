"""One table for "already told them", instead of a column per notice.

Revision ID: 0028
Revises: 0027

Three hand-rolled versions of the same idea sat as columns on the rows they
described: `assigned_notified_at` / `reminded_7d_at` / `reminded_24h_at` on
event_assignment, and `added_notified_at` / `voting_notified_at` on
proposal_voter. Every new notice meant two more columns and a migration — rev
0021 added four — and "who still needs telling" had to OR across all of them, a
shape no plain index can serve, which is why rev 0025 had to add a partial index
spelling the whole predicate out.

`notification` holds one row per (subject, stage) once the notice has gone out.
The nightly jobs ask for the absence of a row instead, which the uniques make an
index lookup, and a fourth notice is a new enum label rather than DDL.

**Deliberately not polymorphic.** A single `(entity_type, entity_id)` pair would
have been shorter and would have thrown away what the old columns got for free:
deleting an assignment took its stamps with it. Two nullable foreign keys keep
the cascade and the referential integrity, and a CHECK insists on exactly one
subject per row.

The reminder *preferences* — `notify_7d` and `notify_24h` — stay on the
assignment. They are settings the volunteer chose at sign-up, not a record of
anything sent.

Existing stamps carry across, so nobody who has already had a notice gets it
again on the first night after deploy.
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

STAGES = (
    "event_scheduled",
    "event_week",
    "event_day",
    "roll_added",
    "voting_open",
)

# (stage, source table, source column, subject column)
CARRIED = [
    ("event_scheduled", "event_assignment", "assigned_notified_at", "assignment_id"),
    ("event_week", "event_assignment", "reminded_7d_at", "assignment_id"),
    ("event_day", "event_assignment", "reminded_24h_at", "assignment_id"),
    ("roll_added", "proposal_voter", "added_notified_at", "voter_id"),
    ("voting_open", "proposal_voter", "voting_notified_at", "voter_id"),
]


def upgrade() -> None:
    labels = ", ".join(f"'{s}'" for s in STAGES)
    op.execute(f"CREATE TYPE notification_stage AS ENUM ({labels})")
    op.execute(
        "CREATE TABLE notification ("
        " id serial PRIMARY KEY,"
        " stage notification_stage NOT NULL,"
        " assignment_id integer REFERENCES event_assignment (id) ON DELETE CASCADE,"
        " voter_id integer REFERENCES proposal_voter (id) ON DELETE CASCADE,"
        " sent_at timestamptz NOT NULL DEFAULT now(),"
        " CONSTRAINT uq_notification_assignment UNIQUE (assignment_id, stage),"
        " CONSTRAINT uq_notification_voter UNIQUE (voter_id, stage),"
        " CONSTRAINT ck_notification_subject"
        "   CHECK ((assignment_id IS NULL) <> (voter_id IS NULL)))"
    )

    for stage, table, column, subject in CARRIED:
        op.execute(
            f"INSERT INTO notification (stage, {subject}, sent_at)"
            f" SELECT '{stage}'::notification_stage, id, {column}"
            f" FROM {table} WHERE {column} IS NOT NULL"
        )

    # the partial index rev 0025 added for the OR predicate: the anti-joins are
    # served by uq_notification_assignment now
    op.execute("DROP INDEX ix_event_assignment_owed_notice")
    op.execute(
        "ALTER TABLE event_assignment"
        " DROP COLUMN assigned_notified_at,"
        " DROP COLUMN reminded_7d_at,"
        " DROP COLUMN reminded_24h_at"
    )
    op.execute(
        "ALTER TABLE proposal_voter"
        " DROP COLUMN added_notified_at,"
        " DROP COLUMN voting_notified_at"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE event_assignment"
        " ADD COLUMN assigned_notified_at timestamptz,"
        " ADD COLUMN reminded_7d_at timestamptz,"
        " ADD COLUMN reminded_24h_at timestamptz"
    )
    op.execute(
        "ALTER TABLE proposal_voter"
        " ADD COLUMN added_notified_at timestamptz,"
        " ADD COLUMN voting_notified_at timestamptz"
    )
    for stage, table, column, subject in CARRIED:
        op.execute(
            f"UPDATE {table} t SET {column} = n.sent_at FROM notification n"
            f" WHERE n.{subject} = t.id AND n.stage = '{stage}'::notification_stage"
        )
    op.execute(
        "CREATE INDEX ix_event_assignment_owed_notice ON event_assignment (event_id)"
        " WHERE assigned_notified_at IS NULL"
        " OR (notify_7d AND reminded_7d_at IS NULL)"
        " OR (notify_24h AND reminded_24h_at IS NULL)"
    )
    op.execute("DROP TABLE notification")
    op.execute("DROP TYPE notification_stage")
