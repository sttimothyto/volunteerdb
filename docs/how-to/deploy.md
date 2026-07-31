# Deploy and upgrade production

Production is a containerized podman-Quadlet stack on `sttimothyto-prod`,
deployed by a single idempotent pyinfra script. The same command performs
first-time installs and routine upgrades.

**Prerequisites:** SSH access to the host under a pyinfra inventory name
`sttimothyto-prod`; `uv` locally. For nightly backups, the rclone Google
Drive remote **and its encrypting crypt wrapper** must be provisioned
once — [one-time Drive setup](backup-restore.md#one-time-drive-setup).

## Deploy or upgrade

```sh
uvx pyinfra sttimothyto-prod deploy/deploy.py --dry   # preview
uvx pyinfra sttimothyto-prod deploy/deploy.py -y      # apply
```

Optional environment on the command line:

- `VDB_ADMIN_PASSWORD='…'` — (re)runs the idempotent admin bootstrap for
  `admin@sttimothyto.org`.
- `VDB_SMTP2GO_API_KEY='api-…'` — sets or rotates the outbound-mail key;
  omitted, the existing remote key is kept.

## What the deploy does

1. Installs system packages (podman, `aardvark-dns` — required for
   container-name DNS, curl, rclone).
2. Syncs the repository to `/opt/volunteerdb/app` (the image build context)
   and templates `/etc/volunteerdb/env` and `/etc/volunteerdb/db.env`
   (mode 600). Secrets are self-managing: existing values are read back from
   the remote env file; `VDB_STORAGE_SECRET` and the database password are
   generated once and reused thereafter.
3. Builds `localhost/volunteerdb:latest` on the server.
4. Installs three Quadlet units in `/etc/containers/systemd/` —
   `volunteerdb.network`, `volunteerdb-db.container` (PostgreSQL 17, named
   volume, no published port), `volunteerdb-app.container` (publishes
   `127.0.0.1:8090 → 8080`) — and reloads systemd.
5. Starts the database, then runs **migrations** (`alembic upgrade head`)
   and, if requested, the admin bootstrap as one-shot `podman run --rm`
   containers on the same network with the same image.
6. Restarts `volunteerdb-app.service` onto the fresh image and **smoke
   tests** `http://127.0.0.1:8090/login` until it answers 200.
7. Prunes dangling images.
8. Installs the nightly-backup script and its 02:00 root-crontab entry,
   after asserting the one-time rclone remotes (Google Drive + encrypting
   crypt wrapper) are provisioned
   ([backup how-to](backup-restore.md)). Until that one-time setup is
   done, the deploy fails at this final step — the app itself is already
   fully deployed by then.

TLS and the public hostname are **outside this repository**: Caddy on the
host terminates HTTPS for `vdb.sttimothyto.org` and reverse-proxies to
`127.0.0.1:8090` (`/etc/caddy/Caddyfile`), and DNS has an A record to the
server. See [Production deployment architecture](../explanation/deployment.md).

## Verify

- The deploy's smoke test passed (it fails loudly otherwise).
- `https://vdb.sttimothyto.org/login` loads and a sign-in works.
- On the host: `systemctl status volunteerdb-app volunteerdb-db` — both
  active, health checks passing.

## Roll back an upgrade

The image is rebuilt from the synced source, so rolling back application
code = deploying an older commit:

```sh
git checkout <known-good-commit>
uvx pyinfra sttimothyto-prod deploy/deploy.py -y
git checkout develop
```

Migrations are not automatically downgraded; if the bad release included
one, restore the database from a [backup](backup-restore.md) or run a
reviewed `alembic downgrade` as a one-shot container.

## One-time cutover from the old native install

The script contains a guarded, one-shot **cutover** path that runs only
while the old host-level PostgreSQL (`postgresql@17-main`) is still active:
it stops the native service, dumps the host database to
`/var/backups/volunteerdb-cutover.sql`, restores it into the container
database in a single transaction, and — only after the smoke test passes —
disables the host cluster and retires the old unit file. The full cutover,
rollback, and manual-cleanup runbook is in the `deploy/deploy.py` module
docstring; take a manual backup first:

```sh
ssh sttimothyto-prod 'su - postgres -c "pg_dump volunteerdb" > /root/volunteerdb-manual-backup.sql'
```
