# Write a database migration

Schema changes ship as Alembic revisions in `migrations/versions/`, named
`000N_short_slug.py` in a linear chain. Update the SQLAlchemy models in
`src/volunteerdb/models.py` in the same change — and in the same *order*:
`tests/test_schema_invariants.py` compares the migrated database against the
models column for column and fails if the declaration order and the physical
order have parted company. Append a new column at the end of the class, because
that is where `ALTER TABLE … ADD COLUMN` puts it.

The chain currently starts at `0001`, which is the whole schema in one revision:
revisions `0001`–`0028` were squashed. See
[the migration history](../reference/schema.md#migration-history) for what that
means for a database that predates it.

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

Recipe — the squash absorbed the revisions that used to serve as worked
examples, so it is written out here in full:

1. `op.add_column(<live table>, <new column>)` — appends after `sys_period`.
2. `CREATE TABLE <t>_history_new` with the live columns in order (including
   the new one), then `changed_by integer, op char(1)`.
3. `INSERT INTO <t>_history_new SELECT <old columns>, NULL, changed_by, op
   FROM <t>_history` — pre-existing history rows get NULL for the new
   column (the field didn't exist yet).
4. `DROP TABLE <t>_history; ALTER TABLE <t>_history_new RENAME TO
   <t>_history`.
5. Recreate the indexes: `ix_<t>_history_id` (btree) and
   `ix_<t>_history_sys_period` (GiST) — plus any later per-twin extras
   (`membership_history` also carries `ix_membership_history_volunteer_id`).

**Dropping** a live column follows the same recipe with the drops first:
PostgreSQL skips dropped columns when expanding a row, so the twin must be
rebuilt to the post-drop order, and the old column's history values go with it.
`deploy/catchup-0023.sql` has a worked example — the `volunteer.updated_at`
drop, its twin rebuild and the index recreation, in SQL rather than Python.

No trigger changes are needed — `versioning()` resolves `<table>_history` by
name at runtime. Tables that are *not* versioned (`app_user`,
`custom_field_def`, `app_setting`, and everything under events and elections)
take plain `add_column`.

## Enums, partial indexes, and one trap between them

Closed sets of values are native PostgreSQL enum types, so a new value is
`ALTER TYPE … ADD VALUE` rather than a rewritten CHECK. The trap: PostgreSQL
rebuilds an index when its column's type changes, and a comparison between an
enum column and an *untyped* literal is not `IMMUTABLE` inside an index
predicate. A partial index over an enum column must therefore cast the literal
— `WHERE status = 'open'::proposal_status` — in `models.py` and in any migration
that recreates it.

## Verifying a migration you cannot check by reading

Build both paths and diff them. This is how `deploy/catchup-0023.sql` was
checked against `0001`, and it is worth repeating for any migration whose
correctness is not obvious on the page:

```sh
createdb check_fresh && createdb check_upgraded
VDB_DATABASE_URL=…/check_fresh    uv run alembic upgrade head
VDB_DATABASE_URL=…/check_upgraded uv run alembic upgrade <the revision before>
VDB_DATABASE_URL=…/check_upgraded uv run alembic upgrade head
pg_dump --schema-only --no-owner --no-privileges check_fresh    > fresh.sql
pg_dump --schema-only --no-owner --no-privileges check_upgraded > upgraded.sql
diff <(grep -vE '^--|^$|^SET ' fresh.sql) <(grep -vE '^--|^$|^SET ' upgraded.sql)
```

An empty diff means an upgraded database is indistinguishable from one created
today. A non-empty one is worth reading closely rather than waving through:
constraint names PostgreSQL derived instead of you choosing them, CHECK
expressions carrying a cast to `text` because they were written while the column
was still `varchar`, and column order that differs because the migration
appended where the model declares in the middle are all real, and all of them
surface later as a test failure nobody can place.

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
