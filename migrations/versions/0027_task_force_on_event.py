"""Move the task force onto the event it belongs to, and lose a load-bearing
delete ordering with it.

Revision ID: 0027
Revises: 0026

`event_task_force` was a one-row-per-event side table saying "this event's
team_id is a stand-in; here is the real owner". Two nullable columns on `event`
say the same thing, and say it where every reader already looks.

The reason to move them is the delete behaviour. `event_task_force.team_id`
cascaded from `team`, so deleting the meta team deleted the marker row — which
meant teardown had to repoint the event and FLUSH *before* deleting the team, or
`event.team_id`'s own cascade took the event and its entire attendance record
with it. models.EventTaskForce carried that warning in capitals. On `event` the
columns are ON DELETE SET NULL, so the same delete now blanks a marker instead
of destroying a record, and the ordering is a preference rather than a trap.

`event_task_force_source` stays — a task force has many contributing teams — and
now references `event.id` directly instead of chaining through the marker table.

DELIBERATE DATA LOSS: `created_by` and `created_at` of the task force itself are
dropped. The same facts are in the audit log under `event.collaboration_added`
(both surfaces write it), and the event's own `created_by`/`created_at` are
untouched. At most a handful of rows exist at any moment — a task force lives
from the first collaborator to the event's end.
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE event"
        " ADD COLUMN task_force_team_id integer"
        "   REFERENCES team (id) ON DELETE SET NULL,"
        " ADD COLUMN owner_team_id integer"
        "   REFERENCES team (id) ON DELETE SET NULL"
    )
    op.execute(
        "UPDATE event e SET task_force_team_id = tf.team_id,"
        " owner_team_id = tf.owner_team_id"
        " FROM event_task_force tf WHERE tf.event_id = e.id"
    )
    op.execute(
        # not the name PostgreSQL would pick: the old table's own unique on
        # team_id is already called event_task_force_team_id_key, and it is still
        # standing at this point in the migration
        "ALTER TABLE event ADD CONSTRAINT uq_event_task_force_team"
        " UNIQUE (task_force_team_id)"
    )
    op.execute(
        "ALTER TABLE event ADD CONSTRAINT ck_event_task_force_teams"
        " CHECK (task_force_team_id IS NULL"
        "        OR task_force_team_id <> owner_team_id)"
    )
    op.execute(
        "CREATE INDEX ix_event_owner_team ON event (owner_team_id)"
        " WHERE owner_team_id IS NOT NULL"
    )

    # the sources point at the event now, not at the marker row
    op.execute(
        "ALTER TABLE event_task_force_source"
        " DROP CONSTRAINT event_task_force_source_event_id_fkey"
    )
    op.execute(
        "ALTER TABLE event_task_force_source"
        " ADD CONSTRAINT event_task_force_source_event_id_fkey"
        " FOREIGN KEY (event_id) REFERENCES event (id) ON DELETE CASCADE"
    )

    op.execute("DROP TABLE event_task_force")


def downgrade() -> None:
    op.execute(
        "CREATE TABLE event_task_force ("
        " event_id integer PRIMARY KEY REFERENCES event (id) ON DELETE CASCADE,"
        " team_id integer NOT NULL UNIQUE REFERENCES team (id) ON DELETE CASCADE,"
        " owner_team_id integer NOT NULL REFERENCES team (id) ON DELETE CASCADE,"
        " created_by integer REFERENCES app_user (id) ON DELETE SET NULL,"
        " created_at timestamptz NOT NULL DEFAULT now(),"
        " CONSTRAINT ck_task_force_teams CHECK (team_id <> owner_team_id))"
    )
    op.execute(
        "CREATE INDEX ix_event_task_force_owner ON event_task_force (owner_team_id)"
    )
    # created_by comes back NULL and created_at stamped now(): the values are in
    # the audit log, not here. A row whose owner was deleted (owner_team_id NULL)
    # cannot be represented by the old NOT NULL column and is skipped.
    op.execute(
        "INSERT INTO event_task_force (event_id, team_id, owner_team_id)"
        " SELECT id, task_force_team_id, owner_team_id FROM event"
        " WHERE task_force_team_id IS NOT NULL AND owner_team_id IS NOT NULL"
    )

    op.execute(
        "ALTER TABLE event_task_force_source"
        " DROP CONSTRAINT event_task_force_source_event_id_fkey"
    )
    op.execute(
        "DELETE FROM event_task_force_source s"
        " WHERE NOT EXISTS (SELECT 1 FROM event_task_force tf"
        "                   WHERE tf.event_id = s.event_id)"
    )
    op.execute(
        "ALTER TABLE event_task_force_source"
        " ADD CONSTRAINT event_task_force_source_event_id_fkey"
        " FOREIGN KEY (event_id) REFERENCES event_task_force (event_id)"
        " ON DELETE CASCADE"
    )

    op.execute("DROP INDEX ix_event_owner_team")
    op.execute("ALTER TABLE event DROP CONSTRAINT ck_event_task_force_teams")
    op.execute("ALTER TABLE event DROP CONSTRAINT uq_event_task_force_team")
    op.execute(
        "ALTER TABLE event DROP COLUMN owner_team_id, DROP COLUMN task_force_team_id"
    )
