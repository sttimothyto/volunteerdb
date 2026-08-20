"""Drop the application form and the interest pipeline.

The team's Google Form URL and the public "I'm interested" submissions went
together -- the form was never shown anywhere, it was only ever delivered as
the confirmation email a submission triggered -- so they leave together. The
/ministries/ pages stay; a published page is now the doc and nothing else.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# team_history's live-column prefix after the drop. versioning() archives with
# INSERT ... SELECT ($1).*, so the twin must mirror team column for column and
# in order -- which is why dropping a live column means rebuilding the twin.
# See docs/how-to/write-a-migration.md.
_TWIN_PREFIX = """
	id INTEGER,
	name VARCHAR(200),
	parent_team_id INTEGER,
	description TEXT,
	is_active BOOLEAN,
	sys_period TSTZRANGE,
	workload_weight NUMERIC(8, 2),
	home_doc_url VARCHAR(500)"""

_TWIN_INDEXES = (
    """CREATE INDEX ix_team_history_id ON team_history (id)""",
    """CREATE INDEX ix_team_history_sys_period ON team_history USING gist (sys_period)""",
)


def upgrade() -> None:
    # interest is not versioned, so the table takes uq_interest_open and both
    # CHECK constraints with it
    op.execute("""DROP TABLE interest""")

    op.execute("""ALTER TABLE team DROP COLUMN application_form_url""")
    op.execute(f"""CREATE TABLE team_history_new ({_TWIN_PREFIX},
	changed_by INTEGER,
	op CHAR(1)
)""")
    op.execute(
        """INSERT INTO team_history_new
	SELECT id, name, parent_team_id, description, is_active, sys_period,
	       workload_weight, home_doc_url, changed_by, op
	FROM team_history"""
    )
    op.execute("""DROP TABLE team_history""")
    op.execute("""ALTER TABLE team_history_new RENAME TO team_history""")
    for statement in _TWIN_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    """Restores the shape, not the contents. The dropped column's history values
    went with it and every interest submission is gone by now, so the column
    comes back empty and the table comes back empty."""
    op.execute("""ALTER TABLE team ADD COLUMN application_form_url VARCHAR(500)""")
    op.execute(f"""CREATE TABLE team_history_new ({_TWIN_PREFIX},
	application_form_url VARCHAR(500),
	changed_by INTEGER,
	op CHAR(1)
)""")
    op.execute(
        """INSERT INTO team_history_new
	SELECT id, name, parent_team_id, description, is_active, sys_period,
	       workload_weight, home_doc_url, NULL, changed_by, op
	FROM team_history"""
    )
    op.execute("""DROP TABLE team_history""")
    op.execute("""ALTER TABLE team_history_new RENAME TO team_history""")
    for statement in _TWIN_INDEXES:
        op.execute(statement)

    # verbatim from 0001_initial.py, so the two paths stay diffable
    op.execute("""CREATE TABLE interest (
	id SERIAL NOT NULL,
	team_id INTEGER NOT NULL,
	name VARCHAR(200) NOT NULL,
	email VARCHAR(255) NOT NULL,
	phone VARCHAR(50),
	note TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	resolved_at TIMESTAMP WITH TIME ZONE,
	resolved_by INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT ck_interest_email_lower CHECK (email = lower(email)),
	CONSTRAINT ck_interest_resolution CHECK (resolved_by IS NULL OR resolved_at IS NOT NULL),
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE,
	FOREIGN KEY(resolved_by) REFERENCES app_user (id) ON DELETE SET NULL
)""")
    op.execute(
        """CREATE UNIQUE INDEX uq_interest_open ON interest (team_id, email) WHERE resolved_at IS NULL"""
    )
