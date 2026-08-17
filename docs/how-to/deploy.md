# Deploy and upgrade production

Production is a containerized podman-Quadlet stack, deployed by a single
idempotent pyinfra script. The same command performs first-time installs and
routine upgrades.

This page is the routine path for an instance that already exists. Standing up
a **new** one is [Stand up a new instance](new-instance.md), which covers the
parts this script deliberately does not automate.

**Prerequisites:** a site file in `deploy/sites/` naming the host (see the
[site configuration reference](../reference/site-config.md)), SSH access to
it, and `uv` locally. For nightly backups, the rclone Google Drive remote
**and its encrypting crypt wrapper** must have been provisioned once —
[one-time Drive setup](backup-restore.md#one-time-drive-setup).

## Deploy from CI (normal path)

**Pushing to `main` deploys.** `.github/workflows/ci.yml` runs lint, the test
suite, and the `-W` docs build; if all three pass, the `deploy` job runs the
same pyinfra script below from a clean checkout of that commit. Watch it in
the repository's Actions tab.

Because CI deploys a git checkout rather than a working tree, what is running
in production is exactly one commit — see the warning under
[Deploy or upgrade by hand](#deploy-or-upgrade-by-hand).

The job runs in the `production` GitHub Environment. Which secrets it holds,
and how to create them, is in
[Stand up a new instance](new-instance.md#10-deploy-from-ci-optional). Adding
a required reviewer to that environment turns the deploy into a manual
approval without any workflow change.

To preview from CI without changing anything:

```sh
gh workflow run ci.yml --ref main -f dry_run=true   # runs pyinfra --dry
```

## Deploy or upgrade by hand

The break-glass path, and what CI runs under the hood:

```sh
export VDB_SITE=sttimothy   # a file in deploy/sites/
uvx pyinfra deploy/inventory.py deploy/deploy.py --dry   # preview
uvx pyinfra deploy/inventory.py deploy/deploy.py -y      # apply
```

`VDB_SITE` has no default: on a repository that can deploy more than one
parish, guessing which is a way to deploy the wrong one. The inventory reads
the host from the same file.

```{warning}
A hand-run deploy syncs your **working tree**, not a commit: uncommitted edits
and untracked files ship to production, and the deployed state is no longer
identifiable by SHA. Commit and push instead unless CI is unavailable.
```

Optional environment on the command line:

- `VDB_ADMIN_PASSWORD='…'` — (re)runs the idempotent admin bootstrap for the
  site's `[mail] admin_email`.
- `VDB_SMTP2GO_API_KEY='api-…'` — sets or rotates the outbound-mail key;
  omitted, the existing remote key is kept.

## What the deploy does

Four steps, in `deploy/steps/`, run in this order. pyinfra prints them as
named groups, so its output reads the same way.

**System packages and directories.** podman, `aardvark-dns` (required for
container-name DNS), curl, ca-certificates, rclone.

**Application stack.**

1. Syncs the repository to `/opt/volunteerdb/app` (the image build context)
   and templates `/etc/volunteerdb/env` and `/etc/volunteerdb/db.env`
   (mode 600). Secrets are self-managing: existing values are read back from
   the remote env file; `VDB_STORAGE_SECRET` and the database password are
   generated once and reused thereafter.
2. Builds `localhost/volunteerdb:latest` on the server, passing the site's
   name and contact address as build args so the manual baked into the image
   carries them.
3. Installs three Quadlet units in `/etc/containers/systemd/` —
   `volunteerdb.network`, `volunteerdb-db.container` (PostgreSQL 17, named
   volume, no published port), `volunteerdb-app.container` (publishes
   `127.0.0.1:<listen_port> → 8080`) — and reloads systemd.
4. Starts the database, then runs **migrations** (`alembic upgrade head`)
   and, if requested, the admin bootstrap as one-shot `podman run --rm`
   containers on the same network with the same image.
5. Restarts `volunteerdb-app.service` onto the fresh image and **smoke
   tests** `/login` until it answers 200, then prunes dangling images.

**Nightly backup** and **Drive roster sync.** Each installs its wrapper
script and its systemd timer. They come last deliberately: the backup step
asserts the one-time rclone remotes exist, and on an instance where they do
not, the deploy fails there — with the application already fully deployed.
The remaining nightly jobs run inside the app itself (see
[CLI and jobs](../reference/cli.md)).

TLS and the public hostname are **outside this repository**: Caddy on the host
terminates HTTPS and reverse-proxies to the loopback port, and DNS has an A
record to the server. `deploy/examples/Caddyfile` is the site block to copy;
note that it must carry `encode zstd gzip`, because the app serves no
compressed responses itself. See
[Production deployment architecture](../explanation/deployment.md).

## Verify

- The deploy's smoke test passed (it fails loudly otherwise).
- `https://<your domain>/login` loads and a sign-in works.
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
VDB_SITE=sttimothy uvx pyinfra deploy/inventory.py deploy/deploy.py -y
git checkout main
```

Migrations are not automatically downgraded; if the bad release included
one, restore the database from a [backup](backup-restore.md) or run a
reviewed `alembic downgrade` as a one-shot container.

