# Rotate secrets

- Production secrets live in `/etc/volunteerdb/env` and
  `/etc/volunteerdb/db.env` (mode 600).
- The deploy writes them from values that it reads back on every run. So most
  rotations are "change the value, redeploy".

## SMTP2GO API key

The key on the command line always wins over the stored value:

```sh
VDB_SMTP2GO_API_KEY='api-NEWKEY' make deploy SITE=<your-site>
```

Verify:

1. Trigger a passwordless (OTP) sign-in on the production site.
2. Confirm that the code arrives by email.

## Admin password

```sh
VDB_ADMIN_PASSWORD='new-password' make deploy SITE=<your-site>
```

- This re-runs the idempotent bootstrap for the site's `[mail] admin_email`.
- The bootstrap only creates the account if it is absent. So on a deployment
  that already exists it is a no-op, not a rotation.
- To change the password, sign in as that admin and use `/account` (header
  gear → *Password & sign-in*).
- Or have another admin re-invite the account from `/admin/users` (see
  [Manage user accounts](manage-users.md)).
- In both cases the new password must clear the
  [policy](../explanation/auth.md#what-a-password-has-to-be). A rejected
  `VDB_ADMIN_PASSWORD` fails the bootstrap with the reason.

## Session-cookie secret (`VDB_STORAGE_SECRET`)

The deploy generates this once and reuses it. To rotate it:

1. Edit `/etc/volunteerdb/env` on the server.
2. Replace `VDB_STORAGE_SECRET` with a fresh value
   (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
3. Restart the app:

```sh
systemctl restart volunteerdb-app
```

:::{warning}
A rotation of the storage secret invalidates every session cookie. The app
signs out all users; their accounts are unaffected. API tokens do not use
this secret and continue to work.
:::

## Parish Google token (`VDB_SHEETS_REFRESH_TOKEN`)

- One refresh token serves both the
  [roster-sheet sync](roster-spreadsheets.md) and the
  [parish calendar](google-calendar-sync.md).
- Google revokes it on a password reset of the parish account, and after
  long disuse.
- The first sign is a team page that reports a failed sync. The calendar
  stops its updates at the same moment.

To replace it:

1. On a machine with a browser, sign in as the parish account.
2. Re-authorise with the **same** OAuth client id and secret. A different
   client cannot see the sheets and the calendar that this one created.
   ```sh
   python scripts/google_authorize.py "$CLIENT_ID" "$CLIENT_SECRET"
   ```
3. Pass the new token on one deploy. The deploy reads it back on later runs.
   ```sh
   VDB_SHEETS_REFRESH_TOKEN='…' make deploy SITE=<your-site>
   ```

Verify:

- Click *Sync now* on a team page that has a linked sheet.
- Or run `python -m volunteerdb.jobs.calendar_sync` in the container
  ([Commands and scripts](../reference/cli.md)).

You provision the rclone token and the crypt password behind the nightly
backup by hand. They are outside this page: see
[Back up and restore](backup-restore.md).

## Database password

:::{warning}
This is the one rotation that is **not** automated. `POSTGRES_PASSWORD` in
`db.env` applies only at the first `initdb`. A later change does not change
the role's password inside PostgreSQL.
:::

- The password appears in three places: the role inside PostgreSQL,
  `POSTGRES_PASSWORD` in `db.env`, and `/etc/volunteerdb/env`.
- In `/etc/volunteerdb/env` it appears twice: once as `VDB_DB_PASSWORD` and
  once inside `VDB_DATABASE_URL`.
- They cannot reference each other, because systemd `EnvironmentFile` does no
  `${}` expansion.
- Do not edit all four by hand and hope they agree. Change the two that the
  deploy treats as input, and let the deploy rewrite the rest. It already
  reads `VDB_DB_PASSWORD` back and rebuilds the connection URL from it.

On the server:

```sh
NEW=$(python3 -c "import secrets; print(secrets.token_hex(24))")
podman exec volunteerdb-db psql -U volunteerdb -d volunteerdb \
  -c "ALTER ROLE volunteerdb PASSWORD '$NEW';"
sed -i "s/^VDB_DB_PASSWORD=.*/VDB_DB_PASSWORD=$NEW/" /etc/volunteerdb/env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$NEW/" /etc/volunteerdb/db.env
```

Then redeploy. The deploy rewrites `VDB_DATABASE_URL` to match and restarts
the app:

```sh
make deploy SITE=<your-site>
```

- Between the `ALTER ROLE` and the redeploy, the app cannot reach the
  database for a short time.
- If you would rather not wait, do the third `sed` yourself and run
  `systemctl restart volunteerdb-app`. But then the next deploy is what
  confirms that the two agree, so run one anyway.

Verify with a login on the site and `systemctl status volunteerdb-app`.
