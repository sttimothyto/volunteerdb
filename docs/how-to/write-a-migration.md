# Write a database migration

Schema changes ship as Alembic revisions in `migrations/versions/`, named
`000N_short_slug.py` in a linear chain.

- Update the SQLAlchemy models in `src/volunteerdb/models.py` in the same
  change, and in the same *order*.
- `tests/test_schema_invariants.py` compares the migrated database against
  the models, column for column. It fails if the declaration order and the
  physical order differ.
- Append a new column at the end of the class, because that is where
  `ALTER TABLE … ADD COLUMN` puts it.

The chain currently starts at `0001`, which is the whole schema in one
revision:

- The squash folded revisions `0001`–`0028` into it.
- `0002` is a worked example of a **drop** on a versioned table. It removes
  `team.application_form_url` and rebuilds `team_history` around the gap.
- See [the migration history](../reference/schema.md#migration-history) for
  what the squash means for a database that predates it.

## Ordinary migrations

Create the revision, then apply it:

```sh
uv run alembic revision -m "short description"
uv run alembic upgrade head
```

- Write `upgrade()` and `downgrade()` by hand, between the two commands.
  Autogenerate is not configured against the history twins.
- Test both directions against the dev database before you commit:

```sh
uv run alembic downgrade -1 && uv run alembic upgrade head
```

## Columns on versioned tables: the history-twin rule

:::{important}
`volunteer`, `team`, and `membership` are system-versioned. The
`versioning()` trigger copies rows into `<table>_history` **positionally**
(`INSERT … SELECT ($1).*, changed_by, op`). So a twin must contain the live
columns *in live order*, followed by `changed_by, op`. PostgreSQL can only
append columns, so in the twin a new column would land after
`changed_by`/`op`. Therefore, **to add a live column, you must rebuild the
twin**.
:::

The squash absorbed the revisions that used to serve as worked examples, so
this page writes the recipe out in full:

1. `op.add_column(<live table>, <new column>)`. This appends the column
   after `sys_period`.
2. `CREATE TABLE <t>_history_new` with the live columns in order (with the
   new one), then `changed_by integer, op char(1)`.
3. `INSERT INTO <t>_history_new SELECT <old columns>, NULL, changed_by, op
   FROM <t>_history`. The history rows that are already there get NULL for
   the new column, because the field did not exist yet.
4. `DROP TABLE <t>_history; ALTER TABLE <t>_history_new RENAME TO
   <t>_history`.
5. Recreate the indexes: `ix_<t>_history_id` (btree) and
   `ix_<t>_history_sys_period` (GiST). Add any later per-twin extras
   (`membership_history` also carries `ix_membership_history_volunteer_id`).

To **drop** a live column, follow the same recipe with the drops first:

- PostgreSQL skips dropped columns when it expands a row. So you must
  rebuild the twin to the post-drop order, and the old column's history
  values go with it.
- `migrations/versions/0002_drop_application_form_and_interest.py` is that
  shape in Python.

You need no trigger changes: `versioning()` resolves `<table>_history` by
name at runtime.

Tables that are *not* versioned (`app_user`, `custom_field_def`,
`app_setting`, and everything under events and elections) take a plain
`add_column`.

## Enums, partial indexes, and one trap between them

- Closed sets of values are native PostgreSQL enum types. So a new value is
  `ALTER TYPE … ADD VALUE`, not a rewritten CHECK.
- The trap: PostgreSQL rebuilds an index when the type of its column
  changes. A comparison between an enum column and an *untyped* literal is
  not `IMMUTABLE` inside an index predicate.
- Therefore a partial index over an enum column must cast the literal:
  `WHERE status = 'open'::proposal_status`. Do this in `models.py` and in
  any migration that recreates the index.

## Verifying a migration you cannot check by reading

Build both paths and diff them. This is the check the squash passed.
Repeat it for any migration whose correctness is not obvious on the page:

```sh
createdb check_fresh && createdb check_upgraded
VDB_DATABASE_URL=…/check_fresh    uv run alembic upgrade head
VDB_DATABASE_URL=…/check_upgraded uv run alembic upgrade <the revision before>
VDB_DATABASE_URL=…/check_upgraded uv run alembic upgrade head
pg_dump --schema-only --no-owner --no-privileges check_fresh    > fresh.sql
pg_dump --schema-only --no-owner --no-privileges check_upgraded > upgraded.sql
diff <(grep -vE '^--|^$|^SET ' fresh.sql) <(grep -vE '^--|^$|^SET ' upgraded.sql)
```

- An empty diff means an upgraded database is indistinguishable from one
  created today.
- Read a non-empty diff closely; do not wave it through. Each of these is
  real, and each surfaces later as a test failure that nobody can place:
  - constraint names that PostgreSQL derived, instead of names you chose
  - CHECK expressions that carry a cast to `text`, because somebody wrote
    them while the column was still `varchar`
  - a column order that differs, because the migration appended where the
    model declares in the middle

Consider whether you need a schema change at all. A
[custom field](custom-fields-and-workload.md) usually serves a per-volunteer
attribute better, and it needs no migration.

## Verify

```sh
uv run alembic upgrade head
uv run pytest            # conftest migrates a scratch volunteerdb_test DB
```

For a versioned-table change, also:

1. Update a row of the affected table in the app.
2. Confirm that a fresh row lands in `<table>_history`. An error here means
   the column order drifted.

In production, migrations run automatically during [deploy](deploy.md),
before the app restarts onto the new code.
