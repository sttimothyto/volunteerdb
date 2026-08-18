-- Bring a database at revision 0022 up to revision 0023, in SQL.
--
-- Why this exists: 0023 was committed but never deployed, so the live database
-- sat at 0022 when the migration chain was squashed and its files went away.
-- `deploy/catchup-0023.sql` starts, as its name says, at 0023 — this script is
-- the step in front of it:
--
--     psql "$VDB_DATABASE_URL" -f deploy/catchup-0022.sql   # this file
--     psql "$VDB_DATABASE_URL" -f deploy/catchup-0023.sql
--     alembic stamp 0001
--
-- What 0023 did, and all it did: stage an email change until the *new* address
-- opens a link, so a typo cannot take an account with it. Three nullable columns
-- and one unique constraint; no data migration, nothing to backfill.
--
-- The three columns are added in this order because that is the order 0023
-- appended them, and a versioned table's physical column order is load-bearing
-- (see the note on AppUser in models.py). app_user is not versioned, but the
-- squash was verified by diffing schema dumps, and that only means something if
-- the two paths agree down to column order.
--
-- One transaction: either the database ends at 0023 or it never left 0022.

BEGIN;

-- Refuse to run against anything but 0022, rather than half-applying.
DO $$
DECLARE current_rev text;
BEGIN
    SELECT version_num INTO current_rev FROM alembic_version;
    IF current_rev IS DISTINCT FROM '0022' THEN
        RAISE EXCEPTION
            'this script upgrades revision 0022; this database is at %', current_rev;
    END IF;
END $$;

ALTER TABLE app_user ADD COLUMN pending_email varchar(255);
ALTER TABLE app_user ADD COLUMN email_change_token varchar(64);
ALTER TABLE app_user ADD COLUMN email_change_expires_at timestamptz;

-- Unique because the token is looked up on its own. pending_email is
-- deliberately NOT unique: two people may ask for the same address, and only
-- the first to confirm gets it — app_user.email's own unique constraint,
-- re-checked at confirm time, settles that race.
ALTER TABLE app_user
    ADD CONSTRAINT uq_app_user_email_change_token UNIQUE (email_change_token);

UPDATE alembic_version SET version_num = '0023';

COMMIT;
