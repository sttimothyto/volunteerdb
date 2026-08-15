# Rotate secrets

Production secrets live in `/etc/volunteerdb/env` and
`/etc/volunteerdb/db.env` (mode 600), written by the deploy from values it
reads back on every run — so most rotations are "change the value, redeploy".

## SMTP2GO API key

Passing the key on the command line always wins over the stored value:

```sh
VDB_SMTP2GO_API_KEY='api-NEWKEY' uvx pyinfra sttimothyto-prod deploy/deploy.py -y
```

Verify: trigger a passwordless (OTP) sign-in on the production site and
confirm the code arrives by email.

## Admin password

```sh
VDB_ADMIN_PASSWORD='new-password' uvx pyinfra sttimothyto-prod deploy/deploy.py -y
```

This re-runs the idempotent bootstrap for `admin@sttimothyto.org` — and only
creates the account if it is missing, so on an existing deployment it is a
no-op, not a rotation. To actually change the password: sign in as that admin
and use `/account` (header gear → *Password & sign-in*), or have another admin
re-invite the account from `/admin/users` (see
[Manage user accounts](manage-users.md)). Either way the new password must
clear the [policy](../explanation/auth.md#what-a-password-has-to-be), and a
rejected `VDB_ADMIN_PASSWORD` fails the bootstrap with the reason.

## Session-cookie secret (`VDB_STORAGE_SECRET`)

The deploy generates this once and reuses it. To rotate, edit
`/etc/volunteerdb/env` on the server, replace `VDB_STORAGE_SECRET` with a
fresh value (`python3 -c "import secrets; print(secrets.token_hex(32))"`),
and restart:

```sh
systemctl restart volunteerdb-app
```

:::{warning}
Rotating the storage secret invalidates every session cookie: all users are
signed out (their accounts are unaffected). API tokens do not use this
secret and keep working.
:::

## Database password

:::{warning}
This is the one rotation that is **not** automated. `POSTGRES_PASSWORD` in
`db.env` is applied only at first `initdb`; changing it later does not
change the role's password inside PostgreSQL.
:::

On the server, keeping the role, `db.env`, and the app env in sync:

```sh
NEW=$(python3 -c "import secrets; print(secrets.token_hex(24))")
podman exec volunteerdb-db psql -U volunteerdb -d volunteerdb \
  -c "ALTER ROLE volunteerdb PASSWORD '$NEW';"
# update VDB_DB_PASSWORD and the password inside VDB_DATABASE_URL:
sed -i "s/^VDB_DB_PASSWORD=.*/VDB_DB_PASSWORD=$NEW/" /etc/volunteerdb/env
sed -i "s#^VDB_DATABASE_URL=postgresql+asyncpg://volunteerdb:[^@]*@#VDB_DATABASE_URL=postgresql+asyncpg://volunteerdb:$NEW@#" /etc/volunteerdb/env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$NEW/" /etc/volunteerdb/db.env
systemctl restart volunteerdb-app
```

The deploy reads `VDB_DB_PASSWORD` back from the env file, so subsequent
deploys keep using the new password. Verify with a login on the site and
`systemctl status volunteerdb-app`.
