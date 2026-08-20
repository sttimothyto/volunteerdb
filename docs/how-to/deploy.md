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

:::{note}
Steps 4 and 5 are not simultaneous: the migration runs against a database the
*previous* image is still serving. A revision that only adds is invisible in
that window, but one that **drops** a column the running image still maps —
`0002` drops `team.application_form_url` — makes the old container answer 500
on every page that reads that table until the restart lands. It is tens of
seconds, but run such a deploy attended and at a quiet hour, or stop
`volunteerdb-app.service` first and let the deploy bring it back.
:::

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

## One-time: a database that predates the migration squash

Revisions `0001`–`0028` were collapsed into a single `0001`, so `alembic upgrade
head` cannot move a database still stamped `0023` — that revision is no longer in
the chain, and alembic stops rather than guessing. Any instance last deployed
before the squash needs one hand-run catch-up, once, *before* the next deploy.
Check where it stands, on the host:

```sh
podman exec -i volunteerdb-db psql -U volunteerdb -Atq volunteerdb \
  -c "select version_num from alembic_version"
```

If that says `0001`, this section is already behind you. Otherwise copy the
catch-up scripts to the host — **both** of them if it says `0022`, which is where
this instance actually sat: `0023` was committed but never deployed, so the live
database was a revision behind the chain when the squash happened.

At `0022`, start with the bridge; at `0023`, skip straight to the second command:

```sh
podman exec -i volunteerdb-db psql -U volunteerdb -v ON_ERROR_STOP=1 \
  volunteerdb < catchup-0022.sql        # only if the revision is 0022
```

`catchup-0022.sql` is revision `0023` written out as SQL — three nullable columns
on `app_user` and one unique constraint, no data migration — and it leaves the
database stamped `0023`, which is what the next script expects. Then, in both
cases:

```sh
podman exec -i volunteerdb-db pg_dump -U volunteerdb volunteerdb \
  | gzip > ~/before-catchup-$(date +%F).sql.gz          # first, always
podman exec -i volunteerdb-db psql -U volunteerdb -v ON_ERROR_STOP=1 \
  volunteerdb < catchup-0023.sql
podman run --rm --network volunteerdb --env-file /etc/volunteerdb/env \
  localhost/volunteerdb:latest alembic stamp 0001
```

Each script runs as one transaction and refuses to start against any revision
but its own, so a second invocation is a loud no-op rather than a mess. Together
they apply exactly what `0023`–`0028` did; the result was checked by building
both paths on scratch databases and diffing `pg_dump --schema-only` to nothing —
including the two constraint names PostgreSQL would otherwise have derived
differently, and the now-unused `pg_trgm` extension. Afterwards deploy normally:
`alembic upgrade head` sees `0001` and has nothing to do.

:::{warning}
**Deploy immediately after.** The catch-up drops columns the *running* image
still maps (`volunteer.updated_at`, the notification stamps, the
`event_task_force` table), so between the catch-up and the deploy the old
container answers 500 on any page that touches a volunteer. Stop
`volunteerdb-app.service` before the catch-up and let the deploy start it again,
or run the two back to back and accept a window of a minute or so. Do not begin
if you cannot finish.
:::

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
reviewed `alembic downgrade` as a one-shot container. Note that `0001` is now
the base revision: downgrading it drops every table, so for anything below the
current head, restore from backup instead.

