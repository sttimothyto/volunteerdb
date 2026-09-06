"""The whole schema, in one revision.

Revision ID: 0001
Revises: none

The three core tables and their history twins, custom fields, elections,
events and task forces, team pages, the Drive sheet pointers, the site logo,
the mail counter, the scheduler's job record, and the notification log -- all
stated once, in their finished shape, with the reasoning kept in models.py
beside the columns it belongs to.

The statements below are a frozen snapshot, deliberately spelled out rather than
generated from `models.Base.metadata` at run time: a migration that imports the
application's models stops describing the schema it created the moment those
models change again. They were emitted from the metadata once, and
`tests/test_schema_invariants.py` keeps them honest from here -- it compares the
migrated database against the models on every run.

Column order is the order live databases physically have, and models.py
declares its columns in the same order, so that a fresh database and a live one
diff clean under `pg_dump --schema-only` (docs/how-to/write-a-migration.md).
That is a convention for the reader and the diff, not a mechanism: the
versioning trigger below archives by column name.

pg_trgm is not installed: no index here needs it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

VERSIONED_TABLES = ("volunteer", "team", "membership")

# BEFORE UPDATE/DELETE on each versioned table: close the old row's validity
# period, copy it into the twin, and open a fresh period on the new row. The
# archive INSERT builds a row of the twin's own type from the old row's JSON,
# so columns match by NAME: `to_jsonb(OLD)` carries every live column, the two
# audit fields are appended to the object, and any twin column the object does
# not mention (one the live table has dropped since) is NULL. Order is
# irrelevant, a type mismatch raises, and adding a live column is two ADD
# COLUMNs. See docs/how-to/write-a-migration.md.
VERSIONING_FN = """
CREATE OR REPLACE FUNCTION versioning() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    hist regclass := (quote_ident(TG_TABLE_SCHEMA) || '.'
                      || quote_ident(TG_TABLE_NAME || '_history'))::regclass;
    uid integer := NULLIF(current_setting('app.user_id', true), '')::integer;
    ts timestamptz := clock_timestamp();
BEGIN
    IF TG_OP = 'UPDATE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format(
            'INSERT INTO %s SELECT (jsonb_populate_record(NULL::%s, '
            'to_jsonb($1) || jsonb_build_object(''changed_by'', $2, ''op'', $3))).*',
            hist, hist
        ) USING OLD, uid, 'U';
        NEW.sys_period := tstzrange(ts, NULL);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        OLD.sys_period := tstzrange(lower(OLD.sys_period), ts);
        EXECUTE format(
            'INSERT INTO %s SELECT (jsonb_populate_record(NULL::%s, '
            'to_jsonb($1) || jsonb_build_object(''changed_by'', $2, ''op'', $3))).*',
            hist, hist
        ) USING OLD, uid, 'D';
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;
"""

SCHEMA: list[str] = [
    """CREATE TYPE assignment_kind AS ENUM ('signup', 'assigned', 'sub')""",
    """CREATE TYPE custom_field_type AS ENUM ('text', 'number', 'select', 'date', 'checkbox', 'integer', 'decimal', 'timestamp', 'timestamptz', 'time', 'interval', 'uuid')""",
    """CREATE TYPE event_status AS ENUM ('scheduled', 'cancelled')""",
    """CREATE TYPE notification_stage AS ENUM ('event_scheduled', 'event_week', 'event_day', 'roll_added', 'voting_open')""",
    """CREATE TYPE page_status AS ENUM ('pending', 'ok', 'error')""",
    """CREATE TYPE proposal_status AS ENUM ('open', 'appointed', 'cancelled')""",
    """CREATE TYPE sub_request_status AS ENUM ('open', 'claimed', 'cancelled')""",
    """CREATE TYPE sync_status AS ENUM ('applied', 'unchanged', 'new', 'error')""",
    """CREATE TYPE team_role AS ENUM ('leader', 'second', 'core', 'member')""",
    """CREATE TABLE app_setting (
	key VARCHAR(100) NOT NULL,
	value JSONB NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (key)
)""",
    """CREATE TABLE custom_field_def (
	id SERIAL NOT NULL,
	key VARCHAR(50) NOT NULL,
	label VARCHAR(100) NOT NULL,
	field_type custom_field_type NOT NULL,
	options JSONB,
	show_in_list BOOLEAN DEFAULT false NOT NULL,
	position INTEGER DEFAULT 0 NOT NULL,
	is_active BOOLEAN DEFAULT true NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_custom_field_options CHECK ((field_type = 'select') = (options IS NOT NULL)),
	UNIQUE (key)
)""",
    """CREATE TABLE job_run (
	job_name VARCHAR(100) NOT NULL,
	last_success_on DATE,
	last_attempt_at TIMESTAMP WITH TIME ZONE,
	last_exit_code INTEGER,
	PRIMARY KEY (job_name)
)""",
    """CREATE TABLE mail_quota (
	day DATE NOT NULL,
	sent INTEGER DEFAULT 0 NOT NULL,
	PRIMARY KEY (day)
)""",
    """CREATE TABLE membership_history (
	id INTEGER,
	volunteer_id INTEGER,
	team_id INTEGER,
	role team_role,
	sys_period TSTZRANGE,
	changed_by INTEGER,
	op CHAR(1)
)""",
    """CREATE INDEX ix_membership_history_id ON membership_history (id)""",
    """CREATE INDEX ix_membership_history_sys_period ON membership_history USING gist (sys_period)""",
    """CREATE INDEX ix_membership_history_volunteer_id ON membership_history (volunteer_id)""",
    """CREATE TABLE team (
	id SERIAL NOT NULL,
	name VARCHAR(200) NOT NULL,
	parent_team_id INTEGER,
	description TEXT,
	is_active BOOLEAN DEFAULT true NOT NULL,
	sys_period TSTZRANGE DEFAULT tstzrange(clock_timestamp(), NULL) NOT NULL,
	workload_weight NUMERIC(8, 2) DEFAULT 0 NOT NULL,
	home_doc_url VARCHAR(500),
	PRIMARY KEY (id),
	CONSTRAINT ck_team_sys_period_open CHECK (upper_inf(sys_period)),
	UNIQUE NULLS NOT DISTINCT (parent_team_id, name),
	FOREIGN KEY(parent_team_id) REFERENCES team (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_team_name ON team (name)""",
    """CREATE TABLE team_history (
	id INTEGER,
	name VARCHAR(200),
	parent_team_id INTEGER,
	description TEXT,
	is_active BOOLEAN,
	sys_period TSTZRANGE,
	workload_weight NUMERIC(8, 2),
	home_doc_url VARCHAR(500),
	changed_by INTEGER,
	op CHAR(1)
)""",
    """CREATE INDEX ix_team_history_id ON team_history (id)""",
    """CREATE INDEX ix_team_history_sys_period ON team_history USING gist (sys_period)""",
    """CREATE TABLE volunteer (
	id SERIAL NOT NULL,
	first_name VARCHAR(100) NOT NULL,
	last_name VARCHAR(100) NOT NULL,
	email VARCHAR(255),
	phone VARCHAR(50),
	notes TEXT,
	is_active BOOLEAN DEFAULT true NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	sys_period TSTZRANGE DEFAULT tstzrange(clock_timestamp(), NULL) NOT NULL,
	custom JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_volunteer_email_lower CHECK (email IS NULL OR email = lower(email)),
	CONSTRAINT ck_volunteer_sys_period_open CHECK (upper_inf(sys_period))
)""",
    """CREATE INDEX ix_volunteer_email ON volunteer (email)""",
    """CREATE INDEX ix_volunteer_name ON volunteer (last_name, first_name)""",
    """CREATE TABLE volunteer_history (
	id INTEGER,
	first_name VARCHAR(100),
	last_name VARCHAR(100),
	email VARCHAR(255),
	phone VARCHAR(50),
	notes TEXT,
	is_active BOOLEAN,
	created_at TIMESTAMP WITH TIME ZONE,
	sys_period TSTZRANGE,
	custom JSONB,
	changed_by INTEGER,
	op CHAR(1)
)""",
    """CREATE INDEX ix_volunteer_history_id ON volunteer_history (id)""",
    """CREATE INDEX ix_volunteer_history_sys_period ON volunteer_history USING gist (sys_period)""",
    """CREATE TABLE app_user (
	id SERIAL NOT NULL,
	volunteer_id INTEGER,
	email VARCHAR(255) NOT NULL,
	password_hash VARCHAR(255),
	is_admin BOOLEAN DEFAULT false NOT NULL,
	api_token VARCHAR(64),
	invite_token VARCHAR(64),
	is_active BOOLEAN DEFAULT true NOT NULL,
	last_login_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	otp_hash VARCHAR(255),
	otp_sent_at TIMESTAMP WITH TIME ZONE,
	otp_expires_at TIMESTAMP WITH TIME ZONE,
	otp_attempts INTEGER DEFAULT 0 NOT NULL,
	invite_expires_at TIMESTAMP WITH TIME ZONE,
	confidentiality_agreed_at TIMESTAMP WITH TIME ZONE,
	pending_email VARCHAR(255),
	email_change_token VARCHAR(64),
	email_change_expires_at TIMESTAMP WITH TIME ZONE,
	calendar_token VARCHAR(64),
	PRIMARY KEY (id),
	CONSTRAINT ck_app_user_invite_pair CHECK ((invite_token IS NULL) = (invite_expires_at IS NULL)),
	CONSTRAINT ck_app_user_email_change_triple CHECK ((pending_email IS NULL) = (email_change_token IS NULL) AND (pending_email IS NULL) = (email_change_expires_at IS NULL)),
	CONSTRAINT ck_app_user_otp_pair CHECK ((otp_hash IS NULL) = (otp_expires_at IS NULL)),
	CONSTRAINT ck_app_user_email_lower CHECK (email = lower(email)),
	UNIQUE (volunteer_id),
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE SET NULL,
	UNIQUE (email),
	UNIQUE (api_token),
	UNIQUE (invite_token),
	UNIQUE (email_change_token),
	UNIQUE (calendar_token)
)""",
    """CREATE TABLE site_logo (
	id SERIAL NOT NULL,
	image BYTEA NOT NULL,
	content_type VARCHAR(50) DEFAULT 'image/png' NOT NULL,
	uploaded_by INTEGER,
	uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_site_logo_singleton CHECK (id = 1),
	FOREIGN KEY(uploaded_by) REFERENCES app_user (id) ON DELETE SET NULL
)""",
    """CREATE TABLE membership (
	id SERIAL NOT NULL,
	volunteer_id INTEGER NOT NULL,
	team_id INTEGER NOT NULL,
	role team_role NOT NULL,
	sys_period TSTZRANGE DEFAULT tstzrange(clock_timestamp(), NULL) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_membership_sys_period_open CHECK (upper_inf(sys_period)),
	UNIQUE (volunteer_id, team_id),
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE,
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_membership_team_id ON membership (team_id)""",
    """CREATE TABLE team_page (
	team_id INTEGER NOT NULL,
	html TEXT,
	fetched_at TIMESTAMP WITH TIME ZONE,
	status page_status DEFAULT 'pending' NOT NULL,
	error TEXT,
	PRIMARY KEY (team_id),
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE
)""",
    """CREATE TABLE team_page_image (
	team_id INTEGER NOT NULL,
	seq INTEGER NOT NULL,
	image BYTEA NOT NULL,
	content_type VARCHAR(50) NOT NULL,
	PRIMARY KEY (team_id, seq),
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE
)""",
    """CREATE TABLE team_sheet (
	team_id INTEGER NOT NULL,
	file_id VARCHAR(128),
	file_name VARCHAR(300),
	last_synced_at TIMESTAMP WITH TIME ZONE,
	last_status sync_status,
	last_error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (team_id),
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE,
	UNIQUE (file_id)
)""",
    """CREATE TABLE event (
	id SERIAL NOT NULL,
	team_id INTEGER NOT NULL,
	title VARCHAR(200) NOT NULL,
	description TEXT,
	location VARCHAR(200),
	starts_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status event_status DEFAULT 'scheduled' NOT NULL,
	cancelled_at TIMESTAMP WITH TIME ZONE,
	cancelled_by INTEGER,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	google_event_id VARCHAR(1024),
	google_fingerprint VARCHAR(64),
	series_id UUID,
	task_force_team_id INTEGER,
	owner_team_id INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT ck_event_times CHECK (starts_at < ends_at),
	CONSTRAINT ck_event_cancelled_at CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
	CONSTRAINT ck_event_task_force_teams CHECK (task_force_team_id IS NULL OR task_force_team_id <> owner_team_id),
	CONSTRAINT ck_event_task_force_gate CHECK (task_force_team_id IS NULL OR team_id = task_force_team_id),
	CONSTRAINT uq_event_task_force_team UNIQUE (task_force_team_id),
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE,
	FOREIGN KEY(cancelled_by) REFERENCES app_user (id) ON DELETE SET NULL,
	FOREIGN KEY(created_by) REFERENCES app_user (id) ON DELETE SET NULL,
	FOREIGN KEY(task_force_team_id) REFERENCES team (id) ON DELETE SET NULL,
	FOREIGN KEY(owner_team_id) REFERENCES team (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_event_ends_at ON event (ends_at)""",
    """CREATE INDEX ix_event_google_id ON event (google_event_id) WHERE google_event_id IS NOT NULL""",
    """CREATE INDEX ix_event_owner_team ON event (owner_team_id) WHERE owner_team_id IS NOT NULL""",
    """CREATE INDEX ix_event_series ON event (series_id) WHERE series_id IS NOT NULL""",
    """CREATE INDEX ix_event_starts_at ON event (starts_at)""",
    """CREATE INDEX ix_event_team_starts ON event (team_id, starts_at)""",
    """CREATE TABLE proposal (
	id SERIAL NOT NULL,
	team_id INTEGER NOT NULL,
	role team_role NOT NULL,
	status proposal_status DEFAULT 'open' NOT NULL,
	notes TEXT,
	nomination_deadline DATE NOT NULL,
	voting_deadline DATE NOT NULL,
	appointed_candidate_id INTEGER,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	decided_by INTEGER,
	decided_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_proposal_deadlines CHECK (nomination_deadline < voting_deadline),
	CONSTRAINT ck_proposal_decision CHECK ((status = 'open') = (decided_at IS NULL)),
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by) REFERENCES app_user (id) ON DELETE SET NULL,
	FOREIGN KEY(decided_by) REFERENCES app_user (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_proposal_appointed_candidate ON proposal (appointed_candidate_id)""",
    """CREATE INDEX ix_proposal_created_at ON proposal (created_at)""",
    """CREATE INDEX ix_proposal_team_id ON proposal (team_id)""",
    """CREATE UNIQUE INDEX uq_proposal_open ON proposal (team_id, role) WHERE status = 'open'::proposal_status""",
    """CREATE TABLE volunteer_photo (
	volunteer_id INTEGER NOT NULL,
	image BYTEA NOT NULL,
	content_type VARCHAR(50) DEFAULT 'image/jpeg' NOT NULL,
	uploaded_by INTEGER,
	uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (volunteer_id),
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE,
	FOREIGN KEY(uploaded_by) REFERENCES app_user (id) ON DELETE SET NULL
)""",
    """CREATE TABLE event_rsvp (
	id SERIAL NOT NULL,
	event_id INTEGER NOT NULL,
	volunteer_id INTEGER NOT NULL,
	available BOOLEAN NOT NULL,
	note VARCHAR(200),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_event_rsvp UNIQUE (event_id, volunteer_id),
	FOREIGN KEY(event_id) REFERENCES event (id) ON DELETE CASCADE,
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_event_rsvp_volunteer_id ON event_rsvp (volunteer_id)""",
    """CREATE TABLE event_slot (
	id SERIAL NOT NULL,
	event_id INTEGER NOT NULL,
	name VARCHAR(100) NOT NULL,
	capacity SMALLINT,
	position INTEGER DEFAULT 0 NOT NULL,
	description VARCHAR(300),
	PRIMARY KEY (id),
	CONSTRAINT uq_event_slot_name UNIQUE (event_id, name),
	CONSTRAINT uq_slot_event UNIQUE (id, event_id),
	CONSTRAINT ck_event_slot_capacity CHECK (capacity IS NULL OR capacity >= 1),
	FOREIGN KEY(event_id) REFERENCES event (id) ON DELETE CASCADE
)""",
    """CREATE TABLE event_task_force_source (
	event_id INTEGER NOT NULL,
	team_id INTEGER NOT NULL,
	PRIMARY KEY (event_id, team_id),
	FOREIGN KEY(event_id) REFERENCES event (id) ON DELETE CASCADE,
	FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE
)""",
    """CREATE TABLE proposal_candidate (
	id SERIAL NOT NULL,
	proposal_id INTEGER NOT NULL,
	volunteer_id INTEGER NOT NULL,
	note TEXT,
	nominated_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_proposal_candidate UNIQUE (proposal_id, volunteer_id),
	CONSTRAINT uq_candidate_proposal UNIQUE (id, proposal_id),
	FOREIGN KEY(proposal_id) REFERENCES proposal (id) ON DELETE CASCADE,
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE,
	FOREIGN KEY(nominated_by) REFERENCES app_user (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_proposal_candidate_volunteer_id ON proposal_candidate (volunteer_id)""",
    """CREATE TABLE proposal_voter (
	id SERIAL NOT NULL,
	proposal_id INTEGER NOT NULL,
	volunteer_id INTEGER NOT NULL,
	added_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_proposal_voter UNIQUE (proposal_id, volunteer_id),
	CONSTRAINT uq_voter_proposal UNIQUE (id, proposal_id),
	FOREIGN KEY(proposal_id) REFERENCES proposal (id) ON DELETE CASCADE,
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE,
	FOREIGN KEY(added_by) REFERENCES app_user (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_proposal_voter_volunteer_id ON proposal_voter (volunteer_id)""",
    """CREATE TABLE event_assignment (
	id SERIAL NOT NULL,
	slot_id INTEGER NOT NULL,
	event_id INTEGER NOT NULL,
	volunteer_id INTEGER NOT NULL,
	kind assignment_kind NOT NULL,
	assigned_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	attended_override BOOLEAN,
	hours_override NUMERIC(5, 2),
	notify_7d BOOLEAN DEFAULT false NOT NULL,
	notify_24h BOOLEAN DEFAULT true NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_event_assignment UNIQUE (event_id, volunteer_id),
	CONSTRAINT ck_event_assignment_hours CHECK (hours_override IS NULL OR hours_override >= 0),
	CONSTRAINT fk_assignment_slot_event FOREIGN KEY(slot_id, event_id) REFERENCES event_slot (id, event_id) ON DELETE CASCADE,
	FOREIGN KEY(event_id) REFERENCES event (id) ON DELETE CASCADE,
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE,
	FOREIGN KEY(assigned_by) REFERENCES app_user (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_event_assignment_slot_id ON event_assignment (slot_id)""",
    """CREATE INDEX ix_event_assignment_volunteer_id ON event_assignment (volunteer_id)""",
    """CREATE TABLE proposal_ballot (
	id SERIAL NOT NULL,
	proposal_id INTEGER NOT NULL,
	voter_id INTEGER NOT NULL,
	candidate_id INTEGER NOT NULL,
	score SMALLINT NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_proposal_ballot UNIQUE (voter_id, candidate_id),
	CONSTRAINT ck_proposal_ballot_score CHECK (score BETWEEN 0 AND 5),
	CONSTRAINT fk_ballot_voter_proposal FOREIGN KEY(voter_id, proposal_id) REFERENCES proposal_voter (id, proposal_id) ON DELETE CASCADE,
	CONSTRAINT fk_ballot_candidate_proposal FOREIGN KEY(candidate_id, proposal_id) REFERENCES proposal_candidate (id, proposal_id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_proposal_ballot_candidate_id ON proposal_ballot (candidate_id)""",
    """CREATE INDEX ix_proposal_ballot_proposal_id ON proposal_ballot (proposal_id)""",
    """CREATE TABLE event_sub_request (
	id SERIAL NOT NULL,
	assignment_id INTEGER NOT NULL,
	requested_by INTEGER,
	note VARCHAR(200),
	status sub_request_status DEFAULT 'open' NOT NULL,
	claimed_by_volunteer_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	resolved_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_sub_request_resolution CHECK ((status = 'open') = (resolved_at IS NULL)),
	CONSTRAINT ck_sub_request_claimant CHECK (claimed_by_volunteer_id IS NULL OR status = 'claimed'),
	FOREIGN KEY(assignment_id) REFERENCES event_assignment (id) ON DELETE CASCADE,
	FOREIGN KEY(requested_by) REFERENCES app_user (id) ON DELETE SET NULL,
	FOREIGN KEY(claimed_by_volunteer_id) REFERENCES volunteer (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_event_sub_request_claimant ON event_sub_request (claimed_by_volunteer_id)""",
    """CREATE UNIQUE INDEX uq_event_sub_request_open ON event_sub_request (assignment_id) WHERE status = 'open'::sub_request_status""",
    """CREATE TABLE notification (
	id SERIAL NOT NULL,
	stage notification_stage NOT NULL,
	assignment_id INTEGER,
	voter_id INTEGER,
	sent_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	volunteer_id INTEGER NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_notification_assignment UNIQUE (assignment_id, volunteer_id, stage),
	CONSTRAINT uq_notification_voter UNIQUE (voter_id, stage),
	CONSTRAINT ck_notification_subject CHECK ((assignment_id IS NULL) <> (voter_id IS NULL)),
	FOREIGN KEY(assignment_id) REFERENCES event_assignment (id) ON DELETE CASCADE,
	FOREIGN KEY(voter_id) REFERENCES proposal_voter (id) ON DELETE CASCADE,
	FOREIGN KEY(volunteer_id) REFERENCES volunteer (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_notification_volunteer_id ON notification (volunteer_id)""",
    """ALTER TABLE proposal ADD CONSTRAINT fk_proposal_appointed_candidate FOREIGN KEY(appointed_candidate_id) REFERENCES proposal_candidate (id) ON DELETE SET NULL""",
]


def upgrade() -> None:
    for statement in SCHEMA:
        op.execute(statement)
    op.execute(VERSIONING_FN)
    for table in VERSIONED_TABLES:
        op.execute(
            f"CREATE TRIGGER versioning_trigger BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION versioning()"
        )


def downgrade() -> None:
    """Drop everything this created.

    Read from the catalog rather than listing tables: the schema is one revision,
    so there is no half-way state to step back to, and a hand-kept list would be
    one more thing to forget."""
    for table in VERSIONED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS versioning_trigger ON {table}")
    op.execute("DROP FUNCTION IF EXISTS versioning()")
    bind = op.get_bind()
    tables = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                " AND tablename <> 'alembic_version'"
            )
        )
    ]
    for table in tables:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    enum_types = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT t.typname FROM pg_type t JOIN pg_namespace n"
                " ON n.oid = t.typnamespace WHERE t.typtype = 'e'"
                " AND n.nspname = current_schema()"
            )
        )
    ]
    for enum_type in enum_types:
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")
