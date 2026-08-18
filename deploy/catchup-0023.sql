-- Bring a database at revision 0023 up to the squashed 0001.
--
-- Revisions 0001-0028 were collapsed into a single migration, so alembic can no
-- longer walk a live database forward: it has no 0024 to run. This script
-- applies exactly what 0024-0028 did, and then the database is stamped:
--
--     pg_dump ... > backup-before-catchup.sql      # do this first
--     psql "$VDB_DATABASE_URL" -f deploy/catchup-0023.sql
--     alembic stamp 0001
--
-- It is idempotent in the sense that matters: it runs inside one transaction and
-- either completes or leaves the database untouched. It is NOT re-runnable —
-- running it twice fails on the first already-applied change, which is the
-- behaviour you want if you are unsure whether it already ran (check
-- `SELECT version_num FROM alembic_version` instead).
--
-- Verified by building both paths on a scratch database and diffing
-- `pg_dump --schema-only` to nothing: 0023 + this script produces byte-for-byte
-- the schema a fresh `alembic upgrade head` produces.

BEGIN;

-- Refuse to run against anything but 0023, rather than half-applying.
DO $$
DECLARE current_rev text;
BEGIN
    SELECT version_num INTO current_rev FROM alembic_version;
    IF current_rev IS DISTINCT FROM '0023' THEN
        RAISE EXCEPTION
            'this script upgrades revision 0023; this database is at %', current_rev;
    END IF;
END $$;

-- ============================================================================
-- 0024: drop volunteer.updated_at, workload_weight NOT NULL, natural PK on the
--       task-force sources, and the lowercase-email invariants
-- ============================================================================

ALTER TABLE volunteer DROP COLUMN updated_at;

-- The versioning() trigger archives positionally, so the twin has to be rebuilt
-- to the post-drop live order. Values of the dropped column go with it; it meant
-- lower(sys_period), which is still there.
CREATE TABLE volunteer_history_new (
    id integer,
    first_name varchar(100),
    last_name varchar(100),
    email varchar(255),
    phone varchar(50),
    notes text,
    is_active boolean,
    created_at timestamptz,
    sys_period tstzrange,
    custom jsonb,
    changed_by integer,
    op char(1)
);
INSERT INTO volunteer_history_new
    SELECT id, first_name, last_name, email, phone, notes, is_active, created_at,
           sys_period, custom, changed_by, op
    FROM volunteer_history;
DROP TABLE volunteer_history;
ALTER TABLE volunteer_history_new RENAME TO volunteer_history;
CREATE INDEX ix_volunteer_history_id ON volunteer_history (id);
CREATE INDEX ix_volunteer_history_sys_period
    ON volunteer_history USING gist (sys_period);

UPDATE team SET workload_weight = 0 WHERE workload_weight IS NULL;
ALTER TABLE team
    ALTER COLUMN workload_weight SET DEFAULT 0,
    ALTER COLUMN workload_weight SET NOT NULL;

ALTER TABLE event_task_force_source DROP CONSTRAINT event_task_force_source_pkey;
ALTER TABLE event_task_force_source DROP COLUMN id;
ALTER TABLE event_task_force_source DROP CONSTRAINT uq_task_force_source;
ALTER TABLE event_task_force_source
    ADD CONSTRAINT event_task_force_source_pkey PRIMARY KEY (event_id, team_id);

UPDATE interest SET email = lower(email);
UPDATE app_user SET email = lower(email);
UPDATE volunteer SET email = lower(email) WHERE email <> lower(email);
ALTER TABLE interest
    ADD CONSTRAINT ck_interest_email_lower CHECK (email = lower(email));
ALTER TABLE app_user
    ADD CONSTRAINT ck_app_user_email_lower CHECK (email = lower(email));
ALTER TABLE volunteer
    ADD CONSTRAINT ck_volunteer_email_lower
    CHECK (email IS NULL OR email = lower(email));

-- ============================================================================
-- 0025: constraints the code documented, and indexes the queries ask for
-- ============================================================================

DROP INDEX IF EXISTS ix_membership_volunteer_id;
DROP INDEX IF EXISTS ix_interest_team_id;
DROP INDEX IF EXISTS ix_event_sub_request_assignment_id;
DROP INDEX IF EXISTS ix_volunteer_email;
DROP INDEX IF EXISTS ix_volunteer_email_trgm;
DROP INDEX IF EXISTS ix_volunteer_full_name_trgm;

CREATE INDEX ix_volunteer_email_lower ON volunteer (lower(email));
CREATE INDEX ix_team_name ON team (name);
CREATE INDEX ix_proposal_created_at ON proposal (created_at);
CREATE INDEX ix_proposal_appointed_candidate ON proposal (appointed_candidate_id);
CREATE INDEX ix_proposal_candidate_volunteer_id ON proposal_candidate (volunteer_id);
CREATE INDEX ix_event_ends_at ON event (ends_at);
CREATE INDEX ix_event_google_id ON event (google_event_id)
    WHERE google_event_id IS NOT NULL;
CREATE INDEX ix_event_task_force_owner ON event_task_force (owner_team_id);
CREATE INDEX ix_event_sub_request_claimant
    ON event_sub_request (claimed_by_volunteer_id);

-- The two denormalized columns become constrained. Any row already disagreeing
-- would be rejected below rather than silently repaired: it would mean something
-- wrote an id that never matched the row it duplicates, and somebody should see
-- that before a constraint hides it.
ALTER TABLE proposal_voter ADD CONSTRAINT uq_voter_proposal UNIQUE (id, proposal_id);
ALTER TABLE proposal_candidate
    ADD CONSTRAINT uq_candidate_proposal UNIQUE (id, proposal_id);
ALTER TABLE event_slot ADD CONSTRAINT uq_slot_event UNIQUE (id, event_id);

ALTER TABLE proposal_ballot DROP CONSTRAINT proposal_ballot_voter_id_fkey;
ALTER TABLE proposal_ballot DROP CONSTRAINT proposal_ballot_candidate_id_fkey;
ALTER TABLE proposal_ballot DROP CONSTRAINT proposal_ballot_proposal_id_fkey;
ALTER TABLE proposal_ballot ADD CONSTRAINT fk_ballot_voter_proposal
    FOREIGN KEY (voter_id, proposal_id)
    REFERENCES proposal_voter (id, proposal_id) ON DELETE CASCADE;
ALTER TABLE proposal_ballot ADD CONSTRAINT fk_ballot_candidate_proposal
    FOREIGN KEY (candidate_id, proposal_id)
    REFERENCES proposal_candidate (id, proposal_id) ON DELETE CASCADE;

ALTER TABLE event_assignment DROP CONSTRAINT event_assignment_slot_id_fkey;
ALTER TABLE event_assignment ADD CONSTRAINT fk_assignment_slot_event
    FOREIGN KEY (slot_id, event_id)
    REFERENCES event_slot (id, event_id) ON DELETE CASCADE;

ALTER TABLE app_user ADD CONSTRAINT ck_app_user_invite_pair
    CHECK ((invite_token IS NULL) = (invite_expires_at IS NULL));
ALTER TABLE app_user ADD CONSTRAINT ck_app_user_email_change_triple
    CHECK ((pending_email IS NULL) = (email_change_token IS NULL)
           AND (pending_email IS NULL) = (email_change_expires_at IS NULL));
ALTER TABLE app_user ADD CONSTRAINT ck_app_user_otp_pair
    CHECK ((otp_hash IS NULL) = (otp_expires_at IS NULL));
ALTER TABLE interest ADD CONSTRAINT ck_interest_resolution
    CHECK (resolved_by IS NULL OR resolved_at IS NOT NULL);
ALTER TABLE event_task_force ADD CONSTRAINT ck_task_force_teams
    CHECK (team_id <> owner_team_id);

-- ============================================================================
-- 0026: native enums
--
-- The three status CHECKs are added AFTER the type conversions below, not here:
-- a CHECK written against a varchar column keeps a cast to text in its stored
-- expression when the column's type changes under it, and a fresh database has
-- no such cast. Adding them last is what makes the two identical.
-- ============================================================================

DROP INDEX uq_proposal_open;
DROP INDEX uq_event_sub_request_open;

ALTER TABLE proposal DROP CONSTRAINT ck_proposal_status;
ALTER TABLE custom_field_def DROP CONSTRAINT ck_custom_field_def_field_type;
ALTER TABLE event DROP CONSTRAINT ck_event_status;
ALTER TABLE event_assignment DROP CONSTRAINT ck_event_assignment_kind;
ALTER TABLE event_sub_request DROP CONSTRAINT ck_event_sub_request_status;

CREATE TYPE proposal_status AS ENUM ('open', 'appointed', 'cancelled');
CREATE TYPE custom_field_type AS ENUM ('text', 'number', 'select', 'date',
    'checkbox', 'integer', 'decimal', 'timestamp', 'timestamptz', 'time',
    'interval', 'uuid');
CREATE TYPE event_status AS ENUM ('scheduled', 'cancelled');
CREATE TYPE assignment_kind AS ENUM ('signup', 'assigned', 'sub');
CREATE TYPE sub_request_status AS ENUM ('open', 'claimed', 'cancelled');
CREATE TYPE page_status AS ENUM ('pending', 'ok', 'error');
CREATE TYPE sync_status AS ENUM ('applied', 'unchanged', 'new', 'error');

ALTER TABLE proposal ALTER COLUMN status DROP DEFAULT;
ALTER TABLE proposal ALTER COLUMN status TYPE proposal_status
    USING status::proposal_status;
ALTER TABLE proposal ALTER COLUMN status SET DEFAULT 'open'::proposal_status;

ALTER TABLE custom_field_def ALTER COLUMN field_type TYPE custom_field_type
    USING field_type::custom_field_type;

ALTER TABLE event ALTER COLUMN status DROP DEFAULT;
ALTER TABLE event ALTER COLUMN status TYPE event_status USING status::event_status;
ALTER TABLE event ALTER COLUMN status SET DEFAULT 'scheduled'::event_status;

ALTER TABLE event_assignment ALTER COLUMN kind TYPE assignment_kind
    USING kind::assignment_kind;

ALTER TABLE event_sub_request ALTER COLUMN status DROP DEFAULT;
ALTER TABLE event_sub_request ALTER COLUMN status TYPE sub_request_status
    USING status::sub_request_status;
ALTER TABLE event_sub_request
    ALTER COLUMN status SET DEFAULT 'open'::sub_request_status;

ALTER TABLE team_page ALTER COLUMN status DROP DEFAULT;
ALTER TABLE team_page ALTER COLUMN status TYPE page_status USING status::page_status;
ALTER TABLE team_page ALTER COLUMN status SET DEFAULT 'pending'::page_status;

ALTER TABLE team_sheet ALTER COLUMN last_status TYPE sync_status
    USING last_status::sync_status;

-- the literals are cast explicitly: an enum-vs-untyped-literal comparison is
-- not IMMUTABLE inside an index predicate
CREATE UNIQUE INDEX uq_proposal_open ON proposal (team_id, role)
    WHERE status = 'open'::proposal_status;
CREATE UNIQUE INDEX uq_event_sub_request_open ON event_sub_request (assignment_id)
    WHERE status = 'open'::sub_request_status;

-- now the status CHECKs, against the enum types
ALTER TABLE event ADD CONSTRAINT ck_event_cancelled_at
    CHECK ((status = 'cancelled'::event_status) = (cancelled_at IS NOT NULL));
ALTER TABLE event_sub_request ADD CONSTRAINT ck_sub_request_resolution
    CHECK ((status = 'open'::sub_request_status) = (resolved_at IS NULL));
ALTER TABLE event_sub_request ADD CONSTRAINT ck_sub_request_claimant
    CHECK (claimed_by_volunteer_id IS NULL
           OR status = 'claimed'::sub_request_status);
ALTER TABLE proposal ADD CONSTRAINT ck_proposal_decision
    CHECK ((status = 'open'::proposal_status) = (decided_at IS NULL));

-- ============================================================================
-- 0027: the task force moves onto its event
-- ============================================================================

ALTER TABLE event
    ADD COLUMN task_force_team_id integer REFERENCES team (id) ON DELETE SET NULL,
    ADD COLUMN owner_team_id integer REFERENCES team (id) ON DELETE SET NULL;
UPDATE event e SET task_force_team_id = tf.team_id, owner_team_id = tf.owner_team_id
    FROM event_task_force tf WHERE tf.event_id = e.id;
ALTER TABLE event ADD CONSTRAINT uq_event_task_force_team UNIQUE (task_force_team_id);
ALTER TABLE event ADD CONSTRAINT ck_event_task_force_teams
    CHECK (task_force_team_id IS NULL OR task_force_team_id <> owner_team_id);
CREATE INDEX ix_event_owner_team ON event (owner_team_id)
    WHERE owner_team_id IS NOT NULL;

ALTER TABLE event_task_force_source
    DROP CONSTRAINT event_task_force_source_event_id_fkey;
ALTER TABLE event_task_force_source
    ADD CONSTRAINT event_task_force_source_event_id_fkey
    FOREIGN KEY (event_id) REFERENCES event (id) ON DELETE CASCADE;

DROP TABLE event_task_force;

-- Dropping the old table frees the name PostgreSQL would have chosen for the FK
-- added above; it took `event_task_force_team_id_fkey1` while the table stood.
ALTER TABLE event RENAME CONSTRAINT event_task_force_team_id_fkey1
    TO event_task_force_team_id_fkey;

-- ============================================================================
-- 0028: one notification table instead of a column per notice
-- ============================================================================

CREATE TYPE notification_stage AS ENUM ('event_scheduled', 'event_week',
    'event_day', 'roll_added', 'voting_open');
CREATE TABLE notification (
    id serial PRIMARY KEY,
    stage notification_stage NOT NULL,
    assignment_id integer REFERENCES event_assignment (id) ON DELETE CASCADE,
    voter_id integer REFERENCES proposal_voter (id) ON DELETE CASCADE,
    sent_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_notification_assignment UNIQUE (assignment_id, stage),
    CONSTRAINT uq_notification_voter UNIQUE (voter_id, stage),
    CONSTRAINT ck_notification_subject
        CHECK ((assignment_id IS NULL) <> (voter_id IS NULL))
);

-- carry the existing stamps across, so nobody already told gets told again
INSERT INTO notification (stage, assignment_id, sent_at)
    SELECT 'event_scheduled', id, assigned_notified_at
    FROM event_assignment WHERE assigned_notified_at IS NOT NULL;
INSERT INTO notification (stage, assignment_id, sent_at)
    SELECT 'event_week', id, reminded_7d_at
    FROM event_assignment WHERE reminded_7d_at IS NOT NULL;
INSERT INTO notification (stage, assignment_id, sent_at)
    SELECT 'event_day', id, reminded_24h_at
    FROM event_assignment WHERE reminded_24h_at IS NOT NULL;
INSERT INTO notification (stage, voter_id, sent_at)
    SELECT 'roll_added', id, added_notified_at
    FROM proposal_voter WHERE added_notified_at IS NOT NULL;
INSERT INTO notification (stage, voter_id, sent_at)
    SELECT 'voting_open', id, voting_notified_at
    FROM proposal_voter WHERE voting_notified_at IS NOT NULL;

ALTER TABLE event_assignment
    DROP COLUMN assigned_notified_at,
    DROP COLUMN reminded_7d_at,
    DROP COLUMN reminded_24h_at;
ALTER TABLE proposal_voter
    DROP COLUMN added_notified_at,
    DROP COLUMN voting_notified_at;

-- ============================================================================
-- Normalising two names and one extension, so a migrated database and a fresh
-- one are the same database. None of these changes behaviour.
-- ============================================================================

-- rev 0023 named this explicitly; the model expresses it as `unique=True` on the
-- column, which is the name PostgreSQL derives
ALTER TABLE app_user RENAME CONSTRAINT uq_app_user_email_change_token
    TO app_user_email_change_token_key;

-- rev 0005 installed pg_trgm for two GIN indexes on volunteer; 0025 dropped them
-- because the access-control rewrite of services/volunteers.search had put them
-- behind an OR no index could serve. Nothing depends on it now.
DROP EXTENSION IF EXISTS pg_trgm;

COMMIT;
