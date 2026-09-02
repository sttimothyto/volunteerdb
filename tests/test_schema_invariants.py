"""Structural invariants the source documents repeatedly but nothing enforced.

The PL/pgSQL versioning() trigger archives rows positionally (INSERT ... SELECT
($1).*), so a history twin must mirror its live table column for column, in
order. models.py, both history migrations, docs/how-to/write-a-migration.md and
the README all say so; these are the first checks that would actually notice.
"""

import sqlalchemy as sa

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


async def test_history_twins_mirror_live_column_order(database):
    async with database.connect() as conn:
        for model, twin in HISTORY_TABLES.items():
            live_name = model.__tablename__
            live = [
                tuple(r) for r in await conn.execute(_COLUMN_META, {"table": live_name})
            ]
            hist = [
                tuple(r) for r in await conn.execute(_COLUMN_META, {"table": twin.name})
            ]
            assert live, f"no columns found for {live_name}"
            n = len(live)

            assert hist[:n] == live, (
                f"{twin.name} must mirror {live_name} column for column and in order — "
                "versioning() archives rows with INSERT ... SELECT ($1).*, so any drift "
                "writes values into the wrong columns with no error. Rebuild the twin: "
                "see docs/how-to/write-a-migration.md."
            )
            assert [c[0] for c in hist[n:]] == ["changed_by", "op"], (
                f"{twin.name} must end with exactly the two audit columns, after the "
                f"{n} mirrored ones"
            )
            assert [c[1] for c in hist[n:]] == ["int4", "bpchar"]


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

    Nothing checked this before: the test database is built by a real
    `alembic upgrade head` (conftest), never `create_all`, so ORM/DDL drift was
    invisible — and there *was* drift, in event_assignment, because rev 0021
    appended columns the model declared in the middle.

    It matters more now. Revisions 0001-0028 were squashed into one file whose
    statements are a frozen snapshot rather than generated from the metadata, so
    this is what stops the snapshot and the models parting company.
    """
    async with database.connect() as conn:
        for table in Base.metadata.sorted_tables:
            migrated = [
                (r.column_name, r.udt_name)
                for r in await conn.execute(_COLUMN_META, {"table": table.name})
            ]
            assert migrated, f"the migration creates no table {table.name!r}"
            declared = [c.name for c in table.columns]
            assert [name for name, _ in migrated] == declared, (
                f"{table.name}: the migration's column order differs from "
                f"models.py. Positional history archiving depends on this for "
                f"versioned tables, and the squash's dump-diff verification "
                f"depends on it for all of them.\n"
                f"  migrated: {[name for name, _ in migrated]}\n"
                f"  declared: {declared}"
            )

        # and no table exists that the models do not describe
        rows = await conn.execute(
            sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename <> 'alembic_version'"
            )
        )
        in_database = {r.tablename for r in rows}
        declared_tables = set(Base.metadata.tables)
        assert in_database == declared_tables, (
            "the migration and models.py disagree about which tables exist: "
            f"only in the database {sorted(in_database - declared_tables)}, "
            f"only in models.py {sorted(declared_tables - in_database)}"
        )
