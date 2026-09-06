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

The chain starts at `0001`, which is the whole schema in one revision. Later
revisions alter it. See [the migration](../reference/schema.md#the-migration)
for what `0001` is and how it is kept honest.

## Ordinary migrations

Create the revision, then apply it:

```sh
uv run alembic revision -m "short description"
uv run alembic upgrade head
```

- Write `upgrade()` and `downgrade()` by hand, between the two commands.
  Autogenerate is not used. `tests/test_schema_invariants.py` runs alembic's
  comparison as a test instead, so a migration that builds less or more than
  the models declare fails there.
- Test both directions against the dev database before you commit:

```sh
uv run alembic downgrade -1 && uv run alembic upgrade head
```

## Columns on versioned tables: the history-twin rule

:::{important}
`volunteer`, `team`, and `membership` are system-versioned. The
`versioning()` trigger copies a row into `<table>_history` **by column name**
(`jsonb_populate_record`). So a twin must carry every live column, under the
same name and type. Order does not matter. Extra columns on the twin do not
matter either.
:::

To **add** a live column, add it to both tables:

```sql
ALTER TABLE team ADD COLUMN motto VARCHAR(200);
ALTER TABLE team_history ADD COLUMN motto VARCHAR(200);
```

- The rows already in the twin get NULL for the new column. The field did
  not exist when they were archived.
- Forget the twin, and `tests/test_schema_invariants.py` fails by name.
  Without that test, the trigger would archive every later version without
  the column, and no error would say so.

To **drop** a live column, drop it from the live table only:

```sql
ALTER TABLE team DROP COLUMN motto;
```

- Keep the twin's column. The archived values stay readable, and the trigger
  writes NULL there from now on.
- Drop it from the twin as well only when nobody wants the old values.

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

Build both paths and diff them, for any migration whose correctness is not
obvious on the page:

```sh
createdb check_fresh && createdb check_upgraded
VDB_DATABASE_URL=…/check_fresh    uv run alembic upgrade head
VDB_DATABASE_URL=…/check_upgraded uv run alembic upgrade <the revision before>
VDB_DATABASE_URL=…/check_upgraded uv run alembic upgrade head
pg_dump --schema-only --no-owner --no-privileges check_fresh    > fresh.sql
pg_dump --schema-only --no-owner --no-privileges check_upgraded > upgraded.sql
diff <(grep -vE '^--|^$|^SET |^\\(un)?restrict ' fresh.sql) \
     <(grep -vE '^--|^$|^SET |^\\(un)?restrict ' upgraded.sql)
```

- An empty diff means an upgraded database is indistinguishable from one
  created today. (The `\restrict` lines are a token pg_dump draws afresh
  for every dump, hence the filter.)
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
2. Confirm that a fresh row lands in `<table>_history`. An error here names
   a live column the twin lacks, or one whose type differs.

In production, migrations run automatically during [deploy](deploy.md),
before the app restarts onto the new code.
