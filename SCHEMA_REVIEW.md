# Schema review, 2026-09-06

A read of `src/volunteerdb/models.py`, the eight revisions under
`migrations/versions/`, the schema tests, and the services that keep the
invariants the schema does not. Head is `0008`; Postgres 17.

The choices that matter are right: invariants held by the database rather
than the services (`tests/test_schema_constraints.py`), no ORM
relationships, denormalised keys pinned by composite foreign keys, and a
deliberate line around what is versioned. What follows is ranked by payoff.
Items 1 and 2 are backed by probes run against the dev container and the
dev database at head; the rest is inference from reading.

## 1. Worth a migration

### 1.1 Archive history rows by name, not position

`versioning()` archives with `INSERT … SELECT ($1).*, $2, $3`. Positional
expansion is why:

- every column change on a versioned table rebuilds the twin and copies its
  whole history (`0002` is the worked example),
- `models.py` carries "keep this the LAST column" comments on `volunteer`,
  `team`, `event_slot` and `app_user`,
- a drifted twin corrupts silently: values land in the wrong columns with
  no error.

`jsonb_populate_record` fills a row type by column *name* and ignores order
([PostgreSQL 17 docs](https://www.postgresql.org/docs/17/functions-json.html#FUNCTIONS-JSON-PROCESSING)).
The trigger body becomes:

```sql
EXECUTE format(
  'INSERT INTO %s SELECT (jsonb_populate_record(NULL::%s,
      to_jsonb($1) || jsonb_build_object(''changed_by'', $2, ''op'', $3))).*',
  hist, hist) USING OLD, uid, 'U';
```

**Verified** in a `BEGIN … ROLLBACK` block on the running PG17 container: a
live table mixing `serial`, `varchar`, a native enum, `numeric(8,2)`,
`tstzrange` and `jsonb`; a twin in scrambled order with one live column
missing and one extra column of its own. Both the UPDATE and the DELETE
archived correctly; ranges, enums and jsonb round-tripped through the type
input functions.

Consequences:

- Adding a live column is two `ADD COLUMN`s (live and twin), no rebuild, no
  history copy.
- A type mismatch raises instead of misplacing.
- The ordering comments in `models.py` and the twin-rebuild recipe in
  `docs/how-to/write-a-migration.md` go away.
- Keep one test: twin column names must be a **superset** of live names. A
  twin missing a live column would drop that value silently. This is a
  one-line change to `test_history_twins_mirror_live_column_order`.

Cost: one `to_jsonb` round trip per archived row. Negligible at parish
scale.

### 1.2 The drift test checks less than its name says

`test_the_migration_builds_the_schema_the_models_describe` fetches
`udt_name` for every column but asserts only names and order. Indexes,
uniques, foreign keys, nullability, types and server defaults are unchecked.

**Verified**: `alembic.autogenerate.compare_metadata` against the dev
database at `0008`, with `compare_type` and `compare_server_default` on,
reports exactly two diffs:

1. the `sys_period` server default on the three versioned tables. A
   spelling difference in reflected DDL, not a real drift;
2. `ix_volunteer_email_lower`, which exists only in the migration.

So a `compare_metadata` assertion with those two allowlisted is viable today
([alembic docs](https://alembic.sqlalchemy.org/en/latest/api/autogenerate.html#alembic.autogenerate.compare_metadata)).
The how-to's note that "autogenerate is not configured against the history
twins" no longer holds: the twins are in `Base.metadata` and compare
cleanly. Gap that remains: alembic does not compare CHECK constraints by
default. Cover those by reflecting `pg_constraint` and comparing names
against the metadata.

Script used:

```python
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from volunteerdb.models import Base


def diff(conn):
    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "compare_server_default": True}
    )
    return compare_metadata(ctx, Base.metadata)
```

### 1.3 Move the email index into the models

`models.py` says SQLAlchemy cannot express the expression index on a mapped
column. It can: `sa.Index("ix_volunteer_email_lower", sa.text("lower(email)"))`
in `__table_args__` compiles to the same `CREATE INDEX … (lower(email))`
(verified by compiling `CreateIndex` for the postgresql dialect).

Better: `ck_volunteer_email_lower` now guarantees the column is lowercase, so
the expression is redundant. A plain `sa.Index("ix_volunteer_email",
"email")` and a query on `Volunteer.email == addr` do the same job, and the
index lives with the model where 1.2 can see it.

The docstring on `services.volunteers.find_by_email` is stale on two counts:
it says mixed-case rows may exist (the CHECK refuses them) and that the
query skips the index (it hits `ix_volunteer_email_lower`).

## 2. Invariants the code keeps but the schema does not state

### 2.1 Task-force team pointer

Sign-up gating reads `event.team_id`; `task_force.add_collaborator` sets it
to the meta team and `teardown` restores it. Nothing states

```sql
CHECK (task_force_team_id IS NULL OR team_id = task_force_team_id)
```

`test_deleting_a_meta_team_no_longer_threatens_its_event` builds the
impossible pair on purpose (`team_id=owner`, `task_force_team_id=meta`) and
would need its setup changed. Note that `event.team_id` still CASCADEs from
`team`; the SET NULL on the marker does not protect the event, the service
guard in `teams.delete` does.

### 2.2 Live rows are open-ended

`history.snapshot` treats a live row whose `sys_period` is closed as gone at
every `t` past the close. The trigger never writes one, but a hand-written
seed or a psql session can. `CHECK (upper_inf(sys_period))` on
`volunteer`, `team` and `membership` costs nothing.

### 2.3 A notification names an assignment, but the assignment's person changes

`claim_sub` and the hand-over path compensate with `_reset_reminders` and,
in the digest branch, a delete of the `event_scheduled` stamp. That works.
A `volunteer_id` on `notification`, with `uq_notification_assignment`
widened to `(assignment_id, volunteer_id, stage)`, would make the
compensating deletes unnecessary: the outgoing person's stamps never match
the incoming one. Design suggestion, not a defect.

### 2.4 Choice fields and their options

`services.custom_fields` enforces "select implies options".
`CHECK ((field_type = 'select') = (options IS NOT NULL))` would put it where
`test_schema_constraints` can refuse the wrong row.

### 2.5 Unindexed foreign keys

The rule the model comments state (`ix_proposal_appointed_candidate`,
`ix_event_sub_request_claimant`): an FK a delete path touches gets an index.
A walk of `Base.metadata` finds eleven FK columns that lead no index,
unique or primary key:

| Table | Column | Target |
|---|---|---|
| `event` | `cancelled_by`, `created_by` | `app_user` |
| `proposal` | `created_by`, `decided_by` | `app_user` |
| `proposal_candidate` | `nominated_by` | `app_user` |
| `proposal_voter` | `added_by` | `app_user` |
| `event_assignment` | `assigned_by` | `app_user` |
| `event_sub_request` | `requested_by` | `app_user` |
| `volunteer_photo`, `site_logo` | `uploaded_by` | `app_user` |
| `event_task_force_source` | `team_id` (second PK column) | `team` |

At parish scale none of this matters. Recommendation: amend the stated rule
to exclude attribution columns rather than add nine indexes.

## 3. ORM hygiene and open questions

### 3.1 Type annotation map

Twenty-one datetime columns each spell out `sa.TIMESTAMP(timezone=True)`. A
bare `Mapped[datetime]` today silently becomes `TIMESTAMP WITHOUT TIME
ZONE`, the same class of bug as the September host-tz CI failures.

```python
class Base(DeclarativeBase):
    type_annotation_map = {datetime: sa.TIMESTAMP(timezone=True)}
```

makes the safe type the default
([SQLAlchemy docs](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html#customizing-the-type-map)).
A companion assertion over `Base.metadata` that no `DateTime` column is
naive is five lines and belongs beside the timezone ratchet.

### 3.2 Enum names versus values

`sa.Enum(SomeStrEnum)` persists member **names**, not values
([SQLAlchemy docs](https://docs.sqlalchemy.org/en/20/core/type_basics.html#sqlalchemy.types.Enum)).
Every enum in `models.py` has name equal to value today (checked all nine),
so nothing is wrong. A member such as `timestamptz = "timestamp with time
zone"` would silently diverge from the Postgres type. Either pass
`values_callable=lambda e: [m.value for m in e]` to each `sa.Enum`, or add a
four-line test asserting name equals value across the module.

### 3.3 Naming convention

Constraint names are mixed: the CHECKs and composite FKs are named, the
simple uniques (`membership`, `team`, every `unique=True`) and single-column
FKs are not. Fine while the 409 handler in `api/deps.py` discards the
constraint name. If a user-facing "already on this team" from the database
error is ever wanted, a `naming_convention` on `Base.metadata` is the
prerequisite.

### 3.4 `FieldType.number`

A float type beside `integer` and `decimal` is the odd one out, and JSON
floats are lossy. If it is legacy, hide it from the new-field picker and
mark it in `FIELD_TYPE_LABELS`. Removing it needs a data migration of
`volunteer.custom`. Question: is anyone still creating `number` fields?

### 3.5 Leader cardinality

Nothing in the schema or the services says a team has at most one leader or
one second. The elections model, one seat per (team, role), implies one. If
that is the rule:

```python
sa.Index(
    "uq_membership_leader",
    "team_id",
    unique=True,
    postgresql_where=sa.text("role = 'leader'::team_role"),
)
```

and the same for `second`. If co-leaders are allowed, the docs should say
so.

Ben: co-leaders are allowed.

### 3.6 Docs drift

- `docs/reference/schema.md`, `proposal_ballot`: says `voter_id` and
  `candidate_id` carry single-column FKs *and* the composite pair. The
  migration has only the composite pair, which is correct and sufficient.
- `models.py`, `Volunteer.email` comment: see 1.3.
- `services/volunteers.find_by_email` docstring: see 1.3.

## If only two

1.1 and 1.2. Together they remove the twin-rebuild recipe from the migration
how-to and make the invariants test cover what the migration actually
builds.
