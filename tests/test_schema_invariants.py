"""Structural invariants the source documents but nothing else enforced.

The versioning() trigger archives rows BY NAME (jsonb_populate_record), so a
history twin must carry every live column under the same name and type -- in
any order, with any extra columns it likes. Beyond the twins: the migrated
database is exactly what models.py describes (alembic's compare_metadata, run
as a test), every CHECK is declared on a model, every datetime column is a
timestamptz, every enum persists its members' values, and every foreign key a
delete scans leads an index, with the exceptions listed beside that test.
"""

from pprint import pformat

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from volunteerdb.audit import REDACTED_COLUMNS
from volunteerdb.models import HISTORY_TABLES, AppUser, Base, ProposalBallot

_COLUMN_META = sa.text(
    """
    SELECT column_name, udt_name, character_maximum_length,
           numeric_precision, numeric_scale
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = :table
     ORDER BY ordinal_position
    """
)


async def _columns(conn, table: str) -> dict[str, tuple]:
    """column name -> (udt, length, precision, scale), in physical order."""
    rows = await conn.execute(_COLUMN_META, {"table": table})
    return {r[0]: tuple(r[1:]) for r in rows}


async def test_history_twins_carry_every_live_column(database):
    """versioning() fills a twin through jsonb_populate_record(NULL::<twin>,
    to_jsonb(OLD) || …), so columns match by NAME. A live column the twin lacks
    would be missing from every archived version without an error; one of a
    different type would raise on the first archive. Both are caught here,
    before either can happen to a real row. Order is deliberately not checked:
    it no longer means anything to the trigger."""
    async with database.connect() as conn:
        for model, twin in HISTORY_TABLES.items():
            live_name = model.__tablename__
            live = await _columns(conn, live_name)
            hist = await _columns(conn, twin.name)
            assert live, f"no columns found for {live_name}"

            missing = sorted(set(live) - set(hist))
            assert not missing, (
                f"{twin.name} lacks {missing}, which {live_name} has -- versioning() "
                "archives by column name, so every later version would be archived "
                "without them. ADD COLUMN on the twin: docs/how-to/write-a-migration.md."
            )
            mistyped = {n: (live[n], hist[n]) for n in live if hist[n] != live[n]}
            assert not mistyped, (
                f"{twin.name} types differ from {live_name} (live, twin): {mistyped}"
            )
            assert hist.get("changed_by", ("?",))[0] == "int4", (
                f"{twin.name} needs an integer changed_by column"
            )
            assert hist.get("op", ("?",))[0] == "bpchar", (
                f"{twin.name} needs a char(1) op column"
            )


async def test_versioning_trigger_is_installed_on_every_versioned_table(database):
    """A migration that adds a versioned table and its twin but forgets the
    trigger loses history silently — nothing fails until someone opens a
    timeline and finds it empty."""
    async with database.connect() as conn:
        rows = await conn.execute(
            sa.text(
                """
                SELECT event_object_table, event_manipulation,
                       action_timing, action_orientation
                  FROM information_schema.triggers
                 WHERE trigger_name = 'versioning_trigger'
                """
            )
        )
        installed = {tuple(r) for r in rows}

    expected = {
        (model.__tablename__, event, "BEFORE", "ROW")
        for model in HISTORY_TABLES
        for event in ("UPDATE", "DELETE")
    }
    assert installed == expected, (
        "every table in models.HISTORY_TABLES needs a BEFORE UPDATE OR DELETE FOR EACH ROW "
        f"versioning_trigger; missing {sorted(expected - installed)}, "
        f"unexpected {sorted(installed - expected)}"
    )


def test_redacted_columns_cover_every_appuser_secret():
    """audit._fmt redacts by column name, so a new credential column is logged
    in the clear until someone remembers to list it."""
    appuser_columns = {c.name for c in AppUser.__table__.columns}
    credentials = {c for c in appuser_columns if c.endswith(("_hash", "_token"))}

    assert credentials <= REDACTED_COLUMNS, (
        f"{sorted(credentials - REDACTED_COLUMNS)} look like credentials, but audit._fmt "
        "would write their values to the log verbatim — add them to audit.REDACTED_COLUMNS "
        "(models.py: 'adding a secret column? add it to audit.REDACTED_COLUMNS')."
    )

    # ballots are secret: the score value must never reach a log line either
    assert "score" in REDACTED_COLUMNS

    secret_columns = appuser_columns | {
        c.name for c in ProposalBallot.__table__.columns
    }
    assert REDACTED_COLUMNS <= secret_columns, (
        f"{sorted(REDACTED_COLUMNS - secret_columns)} are redacted but no longer exist "
        "on app_user or proposal_ballot; prune them so the set keeps documenting what "
        "is actually secret"
    )


async def test_the_migration_builds_the_schema_the_models_describe(database):
    """models.py and the migration must agree, column for column and in order.

    The test database is built by a real `alembic upgrade head` (conftest),
    never `create_all`, so this is what keeps the hand-written DDL and the
    models from parting company. Order is held for the reader, not for the
    trigger: models.py is meant to read as the physical layout, and the
    revisions since the squash were verified by diffing a fresh database
    against an upgraded one, which only means something while the two agree.
    Types, defaults, indexes and constraints are the next test's job.
    """
    async with database.connect() as conn:
        for table in Base.metadata.sorted_tables:
            migrated = list(await _columns(conn, table.name))
            assert migrated, f"the migration creates no table {table.name!r}"
            declared = [c.name for c in table.columns]
            assert migrated == declared, (
                f"{table.name}: the migration's column order differs from "
                f"models.py. Append a new column at the end of the class, where "
                f"ADD COLUMN puts it.\n"
                f"  migrated: {migrated}\n"
                f"  declared: {declared}"
            )

        # and no table exists that the models do not describe
        rows = await conn.execute(
            sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename <> 'alembic_version'"
            )
        )
        in_db = {r.tablename for r in rows}
    declared_tables = set(Base.metadata.tables)
    assert in_db == declared_tables, (
        f"tables in the database but not in models.py: {sorted(in_db - declared_tables)}; "
        f"declared but never created: {sorted(declared_tables - in_db)}"
    )


def _drift(conn) -> list:
    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "compare_server_default": True}
    )
    return compare_metadata(ctx, Base.metadata)


async def test_the_migrated_database_is_exactly_the_metadata(database):
    """alembic's own comparison, run as a test: every column type, nullability
    and server default, every index, unique and foreign key the models declare
    is in the migrated database, and nothing is there that they do not declare.

    Before this, only column names and order were compared, so an index that
    existed in the migration alone (ix_volunteer_email_lower, until 0009) or a
    unique a migration forgot went unnoticed. Constraint names follow
    PostgreSQL's defaults (models.NAMING_CONVENTION) so the comparison is by
    name. CHECKs are not compared by alembic; the next test covers them."""
    async with database.connect() as conn:
        diffs = await conn.run_sync(_drift)
    assert diffs == [], (
        "the migrated database and models.Base.metadata differ:\n" + pformat(diffs)
    )


_CHECKS = sa.text(
    """
    SELECT c.conrelid::regclass::text AS tbl, c.conname
      FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
     WHERE n.nspname = 'public' AND c.contype = 'c'
    """
)


async def test_every_check_constraint_is_declared_on_its_model(database):
    """compare_metadata does not compare CHECK constraints, so this does, by
    name in both directions: one that exists only in a migration is invisible
    to a reader of models.py, and one declared only on a model holds nothing."""
    async with database.connect() as conn:
        in_db = {(r.tbl, r.conname) for r in await conn.execute(_CHECKS)}
    declared = {
        (table.name, str(c.name))
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint)
    }
    assert in_db == declared, (
        f"in the database only: {sorted(in_db - declared)}; "
        f"declared only: {sorted(declared - in_db)}"
    )


def test_every_datetime_column_carries_a_zone():
    """A naive TIMESTAMP is the one type every reader here would mishandle
    (tests/test_purity_layer.py::test_nothing_reads_the_host_timezone is the
    other half of this). Base.type_annotation_map makes timestamptz the
    default for `Mapped[datetime]`; this is what notices an explicit
    `sa.DateTime()` slipping past it."""
    naive = [
        f"{table.name}.{c.name}"
        for table in Base.metadata.tables.values()
        for c in table.columns
        if isinstance(c.type, sa.DateTime) and not c.type.timezone
    ]
    assert naive == [], f"naive datetime columns: {naive}"


def test_every_enum_column_persists_the_members_values():
    """sa.Enum persists member NAMES unless told otherwise; models._pg_enum
    tells it. A column that bypassed the helper would agree with the database
    only for as long as every member's name equals its value."""
    for table in Base.metadata.tables.values():
        for c in table.columns:
            if isinstance(c.type, sa.Enum) and c.type.enum_class is not None:
                values = [m.value for m in c.type.enum_class]
                assert list(c.type.enums) == values, (
                    f"{table.name}.{c.name}: the database labels {c.type.enums} are "
                    f"not the members' values {values} -- declare the type through "
                    "models._pg_enum"
                )


# FK columns that lead no index, on purpose. The rule and its reasoning sit in
# models.py ("Which foreign keys carry an index"): a delete path scans them,
# unless the referenced table is app_user, whose attribution columns are the
# standing exception and are not listed here.
_UNINDEXED_ON_PURPOSE = {
    # second column of the primary key; rows exist only while a task force is
    # live, so the cascade from team scans a table of a few rows
    ("event_task_force_source", "team_id"),
}


def test_every_foreign_key_a_delete_scans_leads_an_index():
    for table in Base.metadata.sorted_tables:
        leading: set[str] = set()
        for index in table.indexes:
            columns = list(index.columns)
            if columns:
                leading.add(columns[0].name)
        for constraint in table.constraints:
            if isinstance(constraint, sa.UniqueConstraint | sa.PrimaryKeyConstraint):
                columns = list(constraint.columns)
                if columns:
                    leading.add(columns[0].name)
        for column in table.columns:
            if not column.foreign_keys:
                continue
            target = next(iter(column.foreign_keys)).column.table.name
            if (
                target == "app_user"
                or (table.name, column.name) in _UNINDEXED_ON_PURPOSE
            ):
                continue
            assert column.name in leading, (
                f"{table.name}.{column.name} references {target}, whose deletes "
                "cascade or SET NULL through it, and no index leads with it -- add "
                "one, or list it in _UNINDEXED_ON_PURPOSE with the reason"
            )
