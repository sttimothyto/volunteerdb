"""Custom fields: core PostgreSQL scalar types

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-17

Widens ck_custom_field_def_field_type to admit integer, decimal, timestamp,
timestamptz, time, interval, and uuid. Only the CHECK constraint moves:
custom_field_def is not system-versioned and values keep living in
volunteer.custom as JSON scalars, so there is no history-twin rebuild and no
data change. The value list matches models.FieldType member order so the live
constraint text equals the metadata's.

Downgrade restores the five-type CHECK and fails by design if any definition
already uses a new type — delete those definitions first.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

OLD = "('text', 'number', 'select', 'date', 'checkbox')"
NEW = (
    "('text', 'number', 'select', 'date', 'checkbox', 'integer', 'decimal',"
    " 'timestamp', 'timestamptz', 'time', 'interval', 'uuid')"
)


def upgrade() -> None:
    op.drop_constraint("ck_custom_field_def_field_type", "custom_field_def")
    op.create_check_constraint(
        "ck_custom_field_def_field_type", "custom_field_def", f"field_type IN {NEW}"
    )


def downgrade() -> None:
    op.drop_constraint("ck_custom_field_def_field_type", "custom_field_def")
    op.create_check_constraint(
        "ck_custom_field_def_field_type", "custom_field_def", f"field_type IN {OLD}"
    )
