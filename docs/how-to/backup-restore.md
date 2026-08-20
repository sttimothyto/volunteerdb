# Back up and restore the database

All state that matters lives in PostgreSQL (the only other persistent app
state is the NiceGUI storage volume, which holds nothing that cannot be
regenerated). A plain-SQL `pg_dump` is a complete backup, including the
history twins.

## Back up

Production (Quadlet container on the server):

```sh
ssh <your-host> \
  'podman exec volunteerdb-db pg_dump -U volunteerdb volunteerdb' \
  > vdb-backup-$(date +%F).sql
```

Development (compose container; find its name with `podman ps`):

```sh
podman exec <db-container> pg_dump -U volunteerdb volunteerdb > backup.sql
```

## Scheduled backups

Production backs itself up nightly. At **02:00 America/Toronto**, the
`volunteerdb-backup.timer` systemd unit (installed by the deploy;
`Persistent=true` makes up a missed night at boot) runs
`/usr/local/bin/volunteerdb-backup`, which:

1. dumps the database (`pg_dump | gzip`, plain SQL format) atomically to
   `/var/backups/volunteerdb/volunteerdb-YYYY-MM-DD.sql.gz`, refusing
   suspiciously small or corrupt archives;
2. uploads it with rclone through the encrypting **crypt** remote
   `volunteerdb-gdrive-backup-crypt` into the Google Drive folder
   `volunteerdb-backups` — file contents *and names* are encrypted on the
   server before upload, so the Drive folder holds only opaque blobs;
3. prunes local copies older than **14 days** and Drive copies older than
   **730 days** (the prune runs against the crypt remote, so it only ever
   touches files the wrapper can decrypt).

The local 14-day copies are deliberately **not** encrypted: they sit
root-only (`0700`) on the same host as the live database, so encrypting
them would add no protection, and plain files keep restores and drills
simple. The Drive leg is where the exposure lives (a shared cloud
account, two years of retention), and that leg is ciphertext.

The 02:00 slot is deliberate: the [Drive roster sync](roster-spreadsheets.md)
runs at **02:30**, so every night's dump is a restore point taken
immediately *before* the only automated bulk write in the system. Restoring
last night's backup undoes a bad sync exactly.

Output goes to journald: `journalctl -u volunteerdb-backup`; manual run:
`systemctl start volunteerdb-backup.service`. On any
failure, an alert email is sent to the site's `[mail] alert_email` via SMTP2GO
(same account the app uses; key read from `/etc/volunteerdb/env` at run
time). The schedule, retention windows and alert address all come from your
site file — see [`[backup]` and `[schedule]`](../reference/site-config.md) —
so change them there and redeploy.

Run a backup on demand with:

```sh
ssh <your-host> 'systemd-cat -t volunteerdb-backup /usr/local/bin/volunteerdb-backup'
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

Then create the crypt wrapper the backup script actually uploads through.
Generate a strong password and create the remote, again as root on the
server (the leading spaces keep the password out of shell history):

```sh
 PW=$(openssl rand -base64 24)
 rclone config create volunteerdb-gdrive-backup-crypt crypt \
   remote=volunteerdb-gdrive-backup:volunteerdb-backups \
   password="$PW" --obscure
 echo "$PW"    # record it (see warning), then clear terminal scrollback
```

:::{warning}
**Record the crypt password off the server, immediately** — in the parish
password manager *and* as a printed copy with the parish records. The
scenario Drive backups exist for (total server loss) is exactly the
scenario in which `rclone.conf` — the only on-server copy of the password
— is gone too. Without the recorded password, every Drive backup is
unreadable ciphertext. To re-read it later while the server is alive:

```sh
rclone reveal "$(sed -n '/^\[volunteerdb-gdrive-backup-crypt\]$/,/^\[/ s/^password = //p' \
  /root/.config/rclone/rclone.conf)"
```
:::

Re-run the deploy afterwards; the assertion passes and the backup timer
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
- No salt (`password2`) is set on the crypt remote: it would be a second
  secret to custody, and it adds nothing against this threat model when
  the password itself is 24 random bytes.
- The crypt remote wraps the same folder the plain remote pointed at.
  Plaintext dumps uploaded before encryption was introduced (2026-07) were
  re-uploaded through the wrapper and the plaintext originals deleted from
  Drive; if you ever see readable `volunteerdb-*.sql.gz` names in the
  Drive web UI, they are unmanaged stragglers — remove them.

## Restore

### Fetch a nightly backup

The last 14 nights are on the server under `/var/backups/volunteerdb/`
(plain `.sql.gz`). Older ones live encrypted on Google Drive; fetch them
**through the crypt remote**, which decrypts transparently and hands back
the plain `.sql.gz`:

```sh
rclone lsl volunteerdb-gdrive-backup-crypt:                       # pick a date
rclone copy volunteerdb-gdrive-backup-crypt:volunteerdb-<DATE>.sql.gz /root/
gunzip /root/volunteerdb-<DATE>.sql.gz
```

(Listing the underlying `volunteerdb-gdrive-backup:volunteerdb-backups`
remote instead shows only encrypted blob names — that view is what anyone
looking at the Drive folder gets.)

Then feed the resulting `.sql` into the commands below.

### Restore without the server (disaster recovery)

If the server is gone, you need two things: the parish Google OAuth
client (plus a browser sign-in as the parish account) and the **recorded
crypt password**. On any machine, recreate both remotes exactly as in
[One-time Drive setup](#one-time-drive-setup) — same names, crypt
wrapping `volunteerdb-gdrive-backup:volunteerdb-backups`, the recorded
password — then fetch as above.

If a fresh `drive.file` grant cannot see the old files (see the
re-provisioning note above), download the encrypted blobs from the Drive
web UI and decrypt them locally by pointing a crypt remote at the
download directory:

```sh
rclone config create vdb-local-crypt crypt \
  remote=/path/to/downloaded-blobs password='<recorded password>' --obscure
rclone copy vdb-local-crypt: ./decrypted/
```

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
