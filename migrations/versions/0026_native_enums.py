"""One mechanism for every closed set of values: native PostgreSQL enums.

Revision ID: 0026
Revises: 0025

Three mechanisms coexisted. `team_role` was a native enum, five columns were
varchar + CHECK, and two — `team_page.status` and `team_sheet.last_status` —
had no constraint at all, so a typo in either was simply stored.

The varchar choice was defended in four places on the grounds that adding a
value would otherwise need `ALTER TYPE`. It has been available since PostgreSQL
9.1, and rev 0018 is the counter-example: widening the custom-field CHECK from
five values to twelve needed DROP + CREATE with the whole list duplicated as
string literals in the migration *and* in models.py, kept in step by hand. The
native type is one `ALTER TYPE … ADD VALUE`.

What this buys in the application is the typing: the columns were `Mapped[str]`,
so `assignment.kind = "signp"` reached the database and failed at flush, a long
way from the line that wrote it. They are `Mapped[AssignmentKind]` and friends
now. Because every one of these is a `StrEnum`, `event.status == "cancelled"`
still reads true and no comparison site had to change.

`team_page.status` and `team_sheet.last_status` gain the values they always
used (`services/pages.py`, `jobs/drive_sync.py`); an out-of-range value in
either would have to be repaired before this runs, and none exists.
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# (type name, values, table, column, old varchar length, server default)
CONVERSIONS = [
    (
        "proposal_status",
        ("open", "appointed", "cancelled"),
        "proposal",
        "status",
        20,
        "open",
    ),
    (
        "custom_field_type",
        (
            "text",
            "number",
            "select",
            "date",
            "checkbox",
            "integer",
            "decimal",
            "timestamp",
            "timestamptz",
            "time",
            "interval",
            "uuid",
        ),
        "custom_field_def",
        "field_type",
        20,
        None,
    ),
    ("event_status", ("scheduled", "cancelled"), "event", "status", 20, "scheduled"),
    (
        "assignment_kind",
        ("signup", "assigned", "sub"),
        "event_assignment",
        "kind",
        20,
        None,
    ),
    (
        "sub_request_status",
        ("open", "claimed", "cancelled"),
        "event_sub_request",
        "status",
        20,
        "open",
    ),
    ("page_status", ("pending", "ok", "error"), "team_page", "status", 20, "pending"),
    (
        "sync_status",
        ("applied", "unchanged", "new", "error"),
        "team_sheet",
        "last_status",
        20,
        None,
    ),
]

# CHECKs the varchar columns carried; the type replaces them
DROPPED_CHECKS = [
    ("proposal", "ck_proposal_status"),
    ("custom_field_def", "ck_custom_field_def_field_type"),
    ("event", "ck_event_status"),
    ("event_assignment", "ck_event_assignment_kind"),
    ("event_sub_request", "ck_event_sub_request_status"),
]


# Partial indexes whose predicate compares one of these columns to a literal.
# PostgreSQL rebuilds an index when its column's type changes, and an
# enum-vs-untyped-literal comparison needs a cast it will not accept as
# IMMUTABLE inside a predicate — so they come down first and go back up with the
# literal cast explicitly.
PARTIAL_INDEXES = [
    (
        "uq_proposal_open",
        "CREATE UNIQUE INDEX uq_proposal_open ON proposal (team_id, role)"
        " WHERE status = 'open'{cast}",
    ),
    (
        "uq_event_sub_request_open",
        "CREATE UNIQUE INDEX uq_event_sub_request_open ON event_sub_request"
        " (assignment_id) WHERE status = 'open'{cast}",
    ),
]
PARTIAL_CASTS = {
    "uq_proposal_open": "::proposal_status",
    "uq_event_sub_request_open": "::sub_request_status",
}


def upgrade() -> None:
    for table, name in DROPPED_CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    for name, _ddl in PARTIAL_INDEXES:
        op.execute(f"DROP INDEX {name}")

    for type_name, values, table, column, _length, default in CONVERSIONS:
        labels = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {type_name} AS ENUM ({labels})")
        # the default has to go before the type changes under it, and come back
        # afterwards typed
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {type_name} USING {column}::{type_name}"
        )
        if default is not None:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} "
                f"SET DEFAULT '{default}'::{type_name}"
            )

    for name, ddl in PARTIAL_INDEXES:
        op.execute(ddl.format(cast=PARTIAL_CASTS[name]))


def downgrade() -> None:
    for name, _ddl in PARTIAL_INDEXES:
        op.execute(f"DROP INDEX {name}")
    for type_name, values, table, column, length, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE varchar({length}) USING {column}::text"
        )
        if default is not None:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'"
            )
        op.execute(f"DROP TYPE {type_name}")

    for name, ddl in PARTIAL_INDEXES:
        op.execute(ddl.format(cast=""))

    # and the CHECKs the types replaced come back
    by_table = {table: values for _t, values, table, _c, _l, _d in CONVERSIONS}
    for table, name in DROPPED_CHECKS:
        column = next(c for _t, _v, tbl, c, _l, _d in CONVERSIONS if tbl == table)
        labels = ", ".join(f"'{v}'" for v in by_table[table])
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({labels}))"
        )
