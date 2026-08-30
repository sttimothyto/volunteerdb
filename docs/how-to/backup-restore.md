# Back up and restore the database

- All state that matters lives in PostgreSQL. The only other persistent app
  state is the NiceGUI storage volume, which holds nothing that the app cannot
  regenerate.
- A plain-SQL `pg_dump` is a complete backup. It includes the history twins.

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

- Production backs itself up nightly.
- The `volunteerdb-backup.timer` systemd unit runs at `[schedule] backup_at`
  in the site's time zone: **02:00 America/Toronto** for the example site.
- The deploy installs the timer. `Persistent=true` makes up a missed night at
  boot.
- The timer runs `/usr/local/bin/volunteerdb-backup`, which:

1. dumps the database (`pg_dump | gzip`, plain SQL format) atomically to
   `/var/backups/volunteerdb/volunteerdb-YYYY-MM-DD.sql.gz`. It refuses
   suspiciously small or corrupt archives;
2. uploads it with rclone through the **crypt** remote
   `volunteerdb-gdrive-backup-crypt` into the Google Drive folder
   `volunteerdb-backups`. The script encrypts file contents *and names* on
   the server before upload, so the Drive folder holds only opaque blobs;
3. prunes local copies older than **14 days** and Drive copies older than
   **730 days**. The prune runs against the crypt remote, so it only ever
   touches files the wrapper can decrypt.

Notes on the schedule:

- The local 14-day copies are **not** encrypted, on purpose. They sit
  root-only (`0700`) on the same host as the live database. Encryption would
  add no protection there, and plain files keep restores and drills simple.
- The Drive leg is where the exposure lives (a shared cloud account, 2 years
  of retention), and that leg is ciphertext.
- The 02:00 slot is deliberate. The
  [Drive roster sync](roster-spreadsheets.md) runs at **02:30**. So every
  night's dump is a restore point taken immediately *before* the only
  automated bulk write in the system. A restore of last night's backup undoes
  a bad sync exactly.
- Output goes to journald: `journalctl -u volunteerdb-backup`. Manual run:
  `systemctl start volunteerdb-backup.service`.
- On any failure, the script sends an alert email to the site's
  `[mail] alert_email` through SMTP2GO. It uses the same account as the app,
  and reads the key from `/etc/volunteerdb/env` at run time.
- The schedule, the retention windows and the alert address all come from
  your site file. See [`[backup]` and `[schedule]`](../reference/site-config.md).
  Change them there and redeploy.
- A site with `[backup] rclone_remote = "none"` skips step 2 and the Drive
  half of step 3. The server still takes, checks and prunes the dump. Nothing
  leaves the server, and every deploy prints a reminder.
- A lost disk then loses the database and its backups together. Treat that
  mode as a stopgap until the Drive setup below is done.

Run a backup on demand with:

```sh
ssh <your-host> 'systemd-cat -t volunteerdb-backup /usr/local/bin/volunteerdb-backup'
```

### One-time Drive setup

- The rclone remote is the only piece the deploy does **not** manage.
  Whenever it refreshes, rclone rewrites the OAuth token inside
  `/root/.config/rclone/rclone.conf`, so a templated config would clobber a
  live token.
- The deploy only asserts that the remote exists. If it does not, the deploy
  fails (after the app is fully deployed) with a pointer here.
- Provision the remote once per server.

Do this on a machine **with a browser**:

1. Build the auth blob and start the authorization. The OAuth client ID and
   secret come from the parish Google Cloud project.
   ```sh
   BLOB=$(printf '%s' '{"client_id":"<CLIENT_ID>","client_secret":"<CLIENT_SECRET>","scope":"drive.file"}' | base64 -w0)
   rclone authorize "drive" "$BLOB"
   ```
   The JSON-blob form is required. The positional
   `rclone authorize drive ID SECRET` form would request the over-broad
   full-`drive` scope.
2. Sign in as the Google account that must own the backups.
3. Approve the request. rclone prints a one-line token JSON.
4. On the server, as root, create the remote. The space at the start of the
   first line keeps the token out of shell history.
   ```sh
    rclone config create volunteerdb-gdrive-backup drive \
      client_id='<CLIENT_ID>' client_secret='<CLIENT_SECRET>' \
      scope=drive.file config_refresh_token=false token='<TOKEN JSON>'
   chmod 600 /root/.config/rclone/rclone.conf
   rclone mkdir volunteerdb-gdrive-backup:volunteerdb-backups   # smoke test + folder
   ```
5. Create the crypt wrapper that the backup script uploads through, again as
   root on the server. The first line generates a strong password. The
   space at the start of each line keeps the password out of shell history.
   ```sh
    PW=$(openssl rand -base64 24)
    rclone config create volunteerdb-gdrive-backup-crypt crypt \
      remote=volunteerdb-gdrive-backup:volunteerdb-backups \
      password="$PW" --obscure
    echo "$PW"    # record it (see warning), then clear terminal scrollback
   ```
6. Record the password (see the warning below).
7. Clear the terminal scrollback.

:::{warning}
**Record the crypt password off the server, immediately.** Put it in the
parish password manager *and* as a printed copy with the parish records.
Drive backups exist for total server loss. In that case `rclone.conf`, the
only on-server copy of the password, is gone too. Without the recorded
password, every Drive backup is unreadable ciphertext. To read it again later,
while the server is alive:

```sh
rclone reveal "$(sed -n '/^\[volunteerdb-gdrive-backup-crypt\]$/,/^\[/ s/^password = //p' \
  /root/.config/rclone/rclone.conf)"
```
:::

Re-run the deploy afterwards. The assertion passes and the deploy installs
the backup timer.

Notes:

- The Google Cloud OAuth consent screen must be **In production** (or
  Internal on Workspace). In *Testing* status, refresh tokens expire after
  7 days and backups start to fail (loudly, through the alert email).
  `drive.file` is a non-sensitive scope, so the switch to production requires
  no review.
- `scope=drive.file` is least-privilege: the token can only see files that
  rclone itself created. Consequence: if you ever re-provision the remote
  under a new grant, backups uploaded under the old grant remain in Drive.
  But they become invisible to `rclone`, and retention no longer covers them.
  Check the Drive web UI for orphans after a re-provision.
- The crypt remote has no salt (`password2`). A salt would be a second secret
  to custody, and it adds nothing against this threat model when the password
  itself is 24 random bytes.
- The crypt remote wraps the same folder that the plain remote pointed at.
  Before 2026-07 the backups went to Drive in plaintext. Those dumps now exist
  in Drive only as re-uploads through the wrapper; the plaintext originals
  are deleted. If you ever see readable `volunteerdb-*.sql.gz` names in the
  Drive web UI, they are unmanaged stragglers. Remove them.

## Restore

### Fetch a nightly backup

- The last 14 nights are on the server under `/var/backups/volunteerdb/`
  (plain `.sql.gz`).
- Older ones live encrypted on Google Drive. Fetch them **through the crypt
  remote**. It decrypts transparently and hands back the plain `.sql.gz`:

```sh
rclone lsl volunteerdb-gdrive-backup-crypt:                       # pick a date
rclone copy volunteerdb-gdrive-backup-crypt:volunteerdb-<DATE>.sql.gz /root/
gunzip /root/volunteerdb-<DATE>.sql.gz
```

- A list of the plain `volunteerdb-gdrive-backup:volunteerdb-backups` remote
  beneath it shows only encrypted blob names. That view is what anyone who looks
  at the Drive folder gets.
- Then feed that `.sql` into the commands below.

### Restore without the server (disaster recovery)

- If the server is gone, you need two things. The first is the parish Google
  OAuth client, plus a browser sign-in as the parish account. The second is
  the **recorded crypt password**.
- On any machine, recreate both remotes exactly as in
  [One-time Drive setup](#one-time-drive-setup): the same names, the crypt
  wrapper around `volunteerdb-gdrive-backup:volunteerdb-backups`, the
  recorded password. Then fetch as above.
- If a fresh `drive.file` grant cannot see the old files (see the
  re-provision note above), download the encrypted blobs from the Drive web
  UI. Then decrypt them locally: point a crypt remote at the download
  directory:

```sh
rclone config create vdb-local-crypt crypt \
  remote=/path/to/downloaded-blobs password='<recorded password>' --obscure
rclone copy vdb-local-crypt: ./decrypted/
```

### Restore into the database

- Restore into an **empty** database.
- For a fresh start, drop and recreate the database first. This destroys the
  current data. Be sure.

```sh
podman exec -i <db-container> psql -U volunteerdb -d postgres \
  -c 'DROP DATABASE volunteerdb;' -c 'CREATE DATABASE volunteerdb;'
podman exec -i <db-container> psql -U volunteerdb -q -d volunteerdb -v ON_ERROR_STOP=1 \
  --single-transaction < backup.sql
```

- In production, stop the app first, so nothing writes mid-restore. Start it
  again afterwards:

```sh
systemctl stop volunteerdb-app
# … restore as above …
systemctl start volunteerdb-app
```

## Verify

- The dump contains the schema, so you need no migration run.
- Check row counts and history:

```sh
podman exec <db-container> psql -U volunteerdb -d volunteerdb -c \
  'SELECT (SELECT count(*) FROM volunteer) AS volunteers,
          (SELECT count(*) FROM membership) AS memberships,
          (SELECT count(*) FROM membership_history) AS history_rows;'
```

- Then sign in and spot-check a team page. Include a "View as of" date from
  before the backup.
