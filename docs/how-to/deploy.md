# Deploy and upgrade production

Production is a containerized podman-Quadlet stack on `sttimothyto-prod`,
deployed by a single idempotent pyinfra script. The same command performs
first-time installs and routine upgrades.

**Prerequisites:** SSH access to the host under a pyinfra inventory name
`sttimothyto-prod`; `uv` locally. For nightly backups, the rclone Google
Drive remote **and its encrypting crypt wrapper** must be provisioned
once — [one-time Drive setup](backup-restore.md#one-time-drive-setup).

## Deploy from CI (normal path)

**Pushing to `main` deploys.** `.github/workflows/ci.yml` runs lint, the test
suite, and the `-W` docs build; if all three pass, the `deploy` job runs the
same pyinfra script below against `sttimothyto-prod` from a clean checkout of
that commit. Watch it in the repository's Actions tab.

Because CI deploys a git checkout rather than a working tree, what is running
in production is exactly one commit — see the warning under
[Deploy or upgrade by hand](#deploy-or-upgrade-by-hand).

The job runs in the `production` GitHub Environment, which holds three
secrets: `DEPLOY_SSH_KEY` (the root SSH key for the host), `DEPLOY_HOST`, and
`DEPLOY_KNOWN_HOSTS` (the host's pinned SSH host key). Adding a required
reviewer to that environment turns the deploy into a manual approval without
any workflow change.

To preview from CI without changing anything:

```sh
gh workflow run ci.yml --ref main -f dry_run=true   # runs pyinfra --dry
```

## Deploy or upgrade by hand

The break-glass path, and what CI runs under the hood:

```sh
uvx pyinfra sttimothyto-prod deploy/deploy.py --dry   # preview
uvx pyinfra sttimothyto-prod deploy/deploy.py -y      # apply
```

```{warning}
A hand-run deploy syncs your **working tree**, not a commit: uncommitted edits
and untracked files ship to production, and the deployed state is no longer
identifiable by SHA. Commit and push instead unless CI is unavailable.
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
code = deploying an older commit. Prefer reverting on `main` so CI redeploys
and history stays honest about what is live:

```sh
git revert <bad-commit> && git push origin main
```

Break-glass, if CI is unavailable:

```sh
git checkout <known-good-commit>
uvx pyinfra sttimothyto-prod deploy/deploy.py -y
git checkout main
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
