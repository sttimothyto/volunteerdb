# Back up and restore the database

All state that matters lives in PostgreSQL (the only other persistent app
state is the NiceGUI storage volume, which holds nothing that cannot be
regenerated). A plain-SQL `pg_dump` is a complete backup, including the
history twins.

## Back up

Production (Quadlet container on the server):

```sh
ssh sttimothyto-prod \
  'podman exec volunteerdb-db pg_dump -U volunteerdb volunteerdb' \
  > vdb-backup-$(date +%F).sql
```

Development (compose container; find its name with `podman ps`):

```sh
podman exec <db-container> pg_dump -U volunteerdb volunteerdb > backup.sql
```

## Scheduled backups

Production backs itself up nightly. At **02:00 America/Toronto**, a root
crontab entry (installed by the deploy) runs
`/usr/local/bin/volunteerdb-backup`, which:

1. dumps the database (`pg_dump | gzip`, plain SQL format) atomically to
   `/var/backups/volunteerdb/volunteerdb-YYYY-MM-DD.sql.gz`, refusing
   suspiciously small or corrupt archives;
2. uploads it with rclone to the Google Drive folder `volunteerdb-backups`
   (remote `volunteerdb-gdrive-backup`);
3. prunes local copies older than **14 days** and Drive copies older than
   **730 days**.

Output goes to journald: `journalctl -t volunteerdb-backup`. On any
failure, an alert email is sent to `admin@sttimothyto.org` via SMTP2GO
(same account the app uses; key read from `/etc/volunteerdb/env` at run
time). The script, schedule, retention windows, and alert address are all
managed in `deploy/deploy.py` — edit the constants there and re-deploy.

Run a backup on demand with:

```sh
ssh sttimothyto-prod 'systemd-cat -t volunteerdb-backup /usr/local/bin/volunteerdb-backup'
```

### One-time Drive setup

The rclone remote is the only piece the deploy does **not** manage:
rclone rewrites the OAuth token inside `/root/.config/rclone/rclone.conf`
whenever it refreshes, so a templated config would clobber a live token.
The deploy only asserts the remote exists and fails (after the app is
fully deployed) with a pointer here if it does not.

Provision it once per server. On a machine **with a browser** (the OAuth
client ID/secret come from the parish Google Cloud project; the JSON-blob
form is required — the positional `rclone authorize drive ID SECRET` form
would request the over-broad full-`drive` scope):

```sh
BLOB=$(printf '%s' '{"client_id":"<CLIENT_ID>","client_secret":"<CLIENT_SECRET>","scope":"drive.file"}' | base64 -w0)
rclone authorize "drive" "$BLOB"
```

Sign in as the Google account that should own the backups and approve;
rclone prints a one-line token JSON. Then on the server, as root (the
leading space keeps the token out of shell history):

```sh
 rclone config create volunteerdb-gdrive-backup drive \
   client_id='<CLIENT_ID>' client_secret='<CLIENT_SECRET>' \
   scope=drive.file config_refresh_token=false token='<TOKEN JSON>'
chmod 600 /root/.config/rclone/rclone.conf
rclone mkdir volunteerdb-gdrive-backup:volunteerdb-backups   # smoke test + folder
```

Re-run the deploy afterwards; the assertion passes and the crontab entry
is installed. Notes:

- The Google Cloud OAuth consent screen must be **In production** (or
  Internal on Workspace) — in *Testing* status, refresh tokens expire
  after 7 days and backups start failing (loudly, via the alert email).
  `drive.file` is a non-sensitive scope, so publishing requires no review.
- `scope=drive.file` is least-privilege: the token can only see files
  rclone itself created. Consequence: if the remote is ever re-provisioned
  under a new grant, backups uploaded under the old one remain in Drive
  but become invisible to `rclone` (retention stops covering them) — check
  the Drive web UI for orphans after re-provisioning.

## Restore

### Fetch a nightly backup

The last 14 nights are on the server under `/var/backups/volunteerdb/`.
Older ones live on Google Drive; from the server (or any machine with the
rclone remote configured):

```sh
rclone lsl volunteerdb-gdrive-backup:volunteerdb-backups          # pick a date
rclone copy volunteerdb-gdrive-backup:volunteerdb-backups/volunteerdb-<DATE>.sql.gz /root/
gunzip /root/volunteerdb-<DATE>.sql.gz
```

Then feed the resulting `.sql` into the commands below.

### Restore into the database

Restore into an **empty** database. For a fresh start, drop and recreate
first (this destroys current data — be sure):

```sh
podman exec -i <db-container> psql -U volunteerdb -d postgres \
  -c 'DROP DATABASE volunteerdb;' -c 'CREATE DATABASE volunteerdb;'
podman exec -i <db-container> psql -U volunteerdb -q -d volunteerdb -v ON_ERROR_STOP=1 \
  --single-transaction < backup.sql
```

In production, stop the app first so nothing writes mid-restore, and start
it again afterwards:

```sh
systemctl stop volunteerdb-app
# … restore as above …
systemctl start volunteerdb-app
```

## Verify

The dump contains the schema, so no migration run is needed. Check row
counts and history:

```sh
podman exec <db-container> psql -U volunteerdb -d volunteerdb -c \
  'SELECT (SELECT count(*) FROM volunteer) AS volunteers,
          (SELECT count(*) FROM membership) AS memberships,
          (SELECT count(*) FROM membership_history) AS history_rows;'
```

then sign in and spot-check a team page, including a "View as of" date
before the backup was taken.
