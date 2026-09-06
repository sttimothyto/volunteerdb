"""The schema states what the services kept.

Four invariants a service, a comment or a test asserted are now constraints,
one index moves to where the drift test can see it, and one mechanism changes
so that a whole class of migration goes away.

**versioning() archives by name.** The trigger used to copy an old row into
its history twin positionally (``INSERT … SELECT ($1).*``). That is why every
column change on volunteer, team or membership meant rebuilding the twin and
copying its whole history (0002 is the worked example), and why a twin whose
order had drifted would file values under the wrong columns without an error.
It now expands the row through ``jsonb_populate_record(NULL::<twin>, …)``,
which fills the twin's row type by column NAME: order is irrelevant, a twin may
keep columns the live table has dropped (their archived values now survive a
drop, where the old recipe lost them), and a type mismatch raises. Adding a
live column is two ADD COLUMNs. The twins themselves are untouched here.

**ix_volunteer_email replaces ix_volunteer_email_lower.** ck_volunteer_email_lower
(0001) guarantees the column is lowercase, so the expression index served
nothing a plain one cannot -- and a plain one is declared on the model, where
tests/test_schema_invariants.py compares it. find_by_email compares directly.

**Four CHECKs**, each something the code relied on:

- volunteer, team, membership: ``upper_inf(sys_period)``. A live row's period
  is open; history.snapshot would treat a closed one as gone.
- event: ``task_force_team_id IS NULL OR team_id = task_force_team_id``. While
  a task force staffs an event, team_id IS the meta team; sign-up gating and
  the mail audience read it.
- custom_field_def: ``(field_type = 'select') = (options IS NOT NULL)``. A
  choice field has options and nothing else does.

Every ADD CONSTRAINT validates the rows already there. The services have never
written one that fails, and a row that does is a finding: the migration stops
on it rather than legalising it. The one exception is custom_field_def.options.
SQLAlchemy's JSONB writes a Python None as the JSON value null unless told
otherwise, so every non-choice field on record carries 'null'::jsonb, which IS
NOT NULL. Those are folded to SQL NULL first, which is what the model writes
from now on (JSONB(none_as_null=True)) and what the CHECK means.

Nothing the running image maps is dropped or renamed, so the previous
container keeps serving while this applies.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

VERSIONED_TABLES = ("volunteer", "team", "membership")

# The archive INSERT builds a row of the twin's own type from the old row's
# JSON, so columns match by name. `to_jsonb(OLD)` carries every live column;
# the two audit fields are appended to the object; any twin column the object
# does not mention (one the live table has dropped since) is NULL.
VERSIONING_FN = """
CREATE OR REPLACE FUNCTION versioning() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    hist regclass := (quote_ident(TG_TABLE_SCHEMA) || '.'
                      || quote_ident(TG_TABLE_NAME || '_history'))::regclass;
    uid integer := NULLIF(current_setting('app.user_id', true), '')::integer;
    ts timestamptz := clock_timestamp();
BEGIN
    IF TG_OP = 'UPDATE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format(
            'INSERT INTO %s SELECT (jsonb_populate_record(NULL::%s, '
            'to_jsonb($1) || jsonb_build_object(''changed_by'', $2, ''op'', $3))).*',
            hist, hist
        ) USING OLD, uid, 'U';
        NEW.sys_period := tstzrange(ts, NULL);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format(
            'INSERT INTO %s SELECT (jsonb_populate_record(NULL::%s, '
            'to_jsonb($1) || jsonb_build_object(''changed_by'', $2, ''op'', $3))).*',
            hist, hist
        ) USING OLD, uid, 'D';
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;
"""

# verbatim from 0001_initial.py: the positional form this revision retires
VERSIONING_FN_POSITIONAL = """
CREATE OR REPLACE FUNCTION versioning() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    hist regclass := (quote_ident(TG_TABLE_SCHEMA) || '.'
                      || quote_ident(TG_TABLE_NAME || '_history'))::regclass;
    uid integer := NULLIF(current_setting('app.user_id', true), '')::integer;
    ts timestamptz := clock_timestamp();
BEGIN
    IF TG_OP = 'UPDATE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format('INSERT INTO %s SELECT ($1).*, $2, $3', hist) USING OLD, uid, 'U';
        NEW.sys_period := tstzrange(ts, NULL);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format('INSERT INTO %s SELECT ($1).*, $2, $3', hist) USING OLD, uid, 'D';
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;
"""


def upgrade() -> None:
    # the triggers resolve the function by name, so replacing the body is the
    # whole change
    op.execute(VERSIONING_FN)

    op.execute("""DROP INDEX ix_volunteer_email_lower""")
    op.execute("""CREATE INDEX ix_volunteer_email ON volunteer (email)""")

    for table in VERSIONED_TABLES:
        op.execute(
            f"""ALTER TABLE {table} ADD CONSTRAINT ck_{table}_sys_period_open
	CHECK (upper_inf(sys_period))"""
        )
    op.execute(
        """ALTER TABLE event ADD CONSTRAINT ck_event_task_force_gate
	CHECK (task_force_team_id IS NULL OR team_id = task_force_team_id)"""
    )
    op.execute(
        """UPDATE custom_field_def SET options = NULL
	WHERE field_type <> 'select' AND options IS NOT NULL"""
    )
    op.execute(
        """UPDATE custom_field_def SET options = '[]'::jsonb
	WHERE field_type = 'select' AND (options IS NULL OR options = 'null'::jsonb)"""
    )
    op.execute(
        """ALTER TABLE custom_field_def ADD CONSTRAINT ck_custom_field_options
	CHECK ((field_type = 'select') = (options IS NOT NULL))"""
    )


def downgrade() -> None:
    """Restores the positional trigger, which still works: this revision left
    every twin mirroring its live table column for column. The CHECKs go; no
    row changes."""
    op.execute(
        """ALTER TABLE custom_field_def DROP CONSTRAINT ck_custom_field_options"""
    )
    op.execute("""ALTER TABLE event DROP CONSTRAINT ck_event_task_force_gate""")
    for table in VERSIONED_TABLES:
        op.execute(
            f"""ALTER TABLE {table} DROP CONSTRAINT ck_{table}_sys_period_open"""
        )
    op.execute("""DROP INDEX ix_volunteer_email""")
    op.execute("""CREATE INDEX ix_volunteer_email_lower ON volunteer (lower(email))""")
    op.execute(VERSIONING_FN_POSITIONAL)
