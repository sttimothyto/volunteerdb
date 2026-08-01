# Write a database migration

Schema changes ship as Alembic revisions in `migrations/versions/`, named
`000N_short_slug.py` in a linear chain. Update the SQLAlchemy models in
`src/volunteerdb/models.py` in the same change.

## Ordinary migrations

```sh
uv run alembic revision -m "short description"
uv run alembic upgrade head
```

Write `upgrade()` and `downgrade()` by hand (autogenerate is not configured
against the history twins). Test both directions against the dev database
before committing:

```sh
uv run alembic downgrade -1 && uv run alembic upgrade head
```

## Columns on versioned tables: the history-twin rule

:::{important}
`volunteer`, `team`, and `membership` are system-versioned. The
`versioning()` trigger copies rows into `<table>_history` **positionally**
(`INSERT … SELECT ($1).*, changed_by, op`), so a twin must contain the live
columns *in live order* followed by `changed_by, op`. PostgreSQL can only
append columns — in the twin they would land after `changed_by`/`op` — so
**adding a live column means rebuilding the twin**.
:::

Recipe (from migration `0002`, which is the worked example to copy):

1. `op.add_column(<live table>, <new column>)` — appends after `sys_period`.
2. `CREATE TABLE <t>_history_new` with the live columns in order (including
   the new one), then `changed_by integer, op char(1)`.
3. `INSERT INTO <t>_history_new SELECT <old columns>, NULL, changed_by, op
   FROM <t>_history` — pre-existing history rows get NULL for the new
   column (the field didn't exist yet).
4. `DROP TABLE <t>_history; ALTER TABLE <t>_history_new RENAME TO
   <t>_history`.
5. Recreate the indexes: `ix_<t>_history_id` (btree) and
   `ix_<t>_history_sys_period` (GiST).

No trigger changes are needed — `versioning()` resolves `<table>_history` by
name at runtime. Tables that are *not* versioned (`app_user`,
`custom_field_def`, `app_setting`) take plain `add_column` (see `0003`).

Consider whether a schema change is needed at all: per-volunteer attributes
are usually better served by a [custom field](custom-fields-and-workload.md),
which needs no migration.

## Verify

```sh
uv run alembic upgrade head
uv run pytest            # conftest migrates a scratch volunteerdb_test DB
```

For a versioned-table change, additionally update a row of the affected
table in the app and confirm a fresh row lands in `<table>_history` (an
error here means column order drifted). In production, migrations run
automatically during [deploy](deploy.md), before the app restarts onto the
new code.
