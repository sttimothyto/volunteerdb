"""Resolve the clergy team by name; drop the "planning" app_setting.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-02

0008 stored the clergy team as a `clergy_team_id` under the "planning"
app_setting key, guarded from three directions (set_config refused any team
not named "Clergy"; teams.update refused to rename it away; teams.delete
refused to remove it) because the id lived in JSONB with no FK behind it.
Since the answer was always "the team named Clergy", the roll builder now
looks that name up directly and the setting has no reader. Deleting the row
keeps a stale id from reading as live configuration.

Data only: no schema change, and nothing in the planning tables moves. An
open proposal's roll is already materialised as proposal_voter rows, so no
existing electorate shifts under this.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SETTING_KEY = "planning"
CLERGY_TEAM_NAME = "Clergy"


def upgrade() -> None:
    op.execute(
        sa.text("DELETE FROM app_setting WHERE key = :key").bindparams(key=SETTING_KEY)
    )


def downgrade() -> None:
    # The pre-0009 invariant was that clergy_team_id pointed at the team named
    # "Clergy" (or was null), so the name recovers the id exactly. No such
    # team means the setting was legitimately unset.
    op.execute(
        sa.text(
            """
            INSERT INTO app_setting (key, value, updated_at)
            SELECT :key,
                   jsonb_build_object(
                       'clergy_team_id',
                       (SELECT id FROM team WHERE name = :clergy LIMIT 1)
                   ),
                   now()
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = now()
            """
        ).bindparams(key=SETTING_KEY, clergy=CLERGY_TEAM_NAME)
    )
