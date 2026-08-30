# Deploy and upgrade production

- Production is a containerized podman-Quadlet stack. One idempotent pyinfra
  script deploys it.
- The same command does first-time installs and routine upgrades.
- This page is the routine path for an instance that already exists.
- To stand up a **new** instance, read
  [Stand up a new instance](new-instance.md). It covers the parts this script
  does not automate, on purpose.

**Prerequisites:**

- A site file in `deploy/sites/` that names the host (see the
  [site configuration reference](../reference/site-config.md)).
- SSH access to the host, and `uv` on your own machine.
- For off-site nightly backups, the rclone Google Drive remote **and its crypt
  wrapper** must exist. You provision them once:
  [one-time Drive setup](backup-restore.md#one-time-drive-setup). Skip this
  if the site file says `[backup] rclone_remote = "none"`.

## Deploy from CI (normal path)

- **A push to `main` deploys.** `.github/workflows/ci.yml` runs lint, the
  test suite, and the `-W` docs build.
- If all three pass, the `deploy` job runs the same pyinfra script below from
  a clean checkout of that commit.
- Watch it in the repository's Actions tab.
- CI deploys a git checkout, not a working tree, so exactly one commit runs
  in production. See the warning under
  [Deploy or upgrade by hand](#deploy-or-upgrade-by-hand).
- The job runs in the `production` GitHub Environment.
  [Stand up a new instance](new-instance.md#10-deploy-from-ci-optional) lists
  the secrets it holds and how to create them.
- Add a required reviewer to that environment to turn the deploy into a
  manual approval. That needs no workflow change.

To preview from CI, and change nothing:

```sh
gh workflow run ci.yml --ref main -f dry_run=true   # runs pyinfra --dry
```

## Deploy or upgrade by hand

This is the break-glass path, and what CI runs under the hood:

```sh
make deploy-dry SITE=<your-site>   # preview; a file in deploy/sites/
make deploy SITE=<your-site>       # apply
```

- Both are `VDB_SITE=<site> uvx pyinfra==<pin> deploy/inventory.py
  deploy/deploy.py` with `--dry` or `-y`. The pin lives in the Makefile.
- `SITE` has no default. On a repository that can deploy more than one
  parish, a guess is a way to deploy the wrong one.
- The inventory reads the host from the same file.

```{warning}
A hand-run deploy syncs your **working tree**, not a commit. Uncommitted edits
and untracked files ship to production, and no SHA identifies the deployed
state. Commit and push instead, unless CI is unavailable.
```

Optional environment on the command line:

- `VDB_ADMIN_PASSWORD='…'` — runs (or re-runs) the idempotent admin bootstrap
  for the site's `[mail] admin_email`.
- `VDB_SMTP2GO_API_KEY='api-…'` — sets or rotates the outbound-mail key. If
  you omit it, the deploy keeps the key already on the host.

## What the deploy does

Four steps, in `deploy/steps/`, run in this order. pyinfra prints them as
named groups, so its output reads the same way.

**System packages and directories.** podman, `aardvark-dns` (required for
container-name DNS), curl, ca-certificates, rclone.

**Application stack.**

1. Syncs the repository to `/opt/volunteerdb/app` (the image build context).
   Templates `/etc/volunteerdb/env` and `/etc/volunteerdb/db.env` (mode 600).
   The deploy manages the secrets itself. It reads the values already there
   back from the remote env file. It generates `VDB_STORAGE_SECRET` and the database
   password once, and reuses them after that.
2. Builds `localhost/volunteerdb:latest` on the server. It passes the site's
   name and contact address as build args, so the manual baked into the image
   carries them.
3. Installs three Quadlet units in `/etc/containers/systemd/`, then reloads
   systemd. The units:
   - `volunteerdb.network`
   - `volunteerdb-db.container` (PostgreSQL 17, named volume, no published
     port)
   - `volunteerdb-app.container` (publishes `127.0.0.1:<listen_port> → 8080`)
4. Starts the database. Then runs **migrations** (`alembic upgrade head`)
   and, if requested, the admin bootstrap. Those run as one-shot
   `podman run --rm` containers on the same network with the same image.
5. Restarts `volunteerdb-app.service` onto the fresh image. **Smoke tests**
   `/login` until it answers 200, then prunes dangling images.

:::{note}
Steps 4 and 5 are not simultaneous: the migration runs against a database
that the *previous* image still serves. A revision that only adds is
invisible in that window. One that **drops** a column the live image still
maps is not: `0002` drops `team.application_form_url`. The old container then
answers 500 on every page that reads that table, until the restart lands. The
window is tens of seconds. Run such a deploy attended and at a quiet hour, or
stop `volunteerdb-app.service` first and let the deploy bring it back.
:::

**Reverse proxy (Caddy).**

- Runs only with `[proxy] caddy = true`. Otherwise it prints a note and does
  nothing.
- Asserts that `[host] domain` resolves to `[host] public_ip` from the
  server.
- Installs Caddy from its upstream apt repository.
- Renders `/etc/caddy/Caddyfile` to a candidate file and runs
  `caddy validate` on it *before* it installs it over the live one. It copies
  a hand-written file aside once, as `Caddyfile.bak-pre-managed-<date>`.
- Opens `http`/`https` in firewalld if firewalld runs.
- Reloads Caddy. A reload keeps its certificates, so there is no issuance and
  no downtime.
- Fetches `https://<domain>/login` until it answers 200.

**Nightly backup.**

- Installs the wrapper script and its systemd timer.
- It comes last on purpose. It asserts that the one-time rclone remotes
  exist. On an instance where they do not, the deploy fails there, with the
  application and the proxy already fully deployed.
- With `[backup] rclone_remote = "none"` there is no Drive leg to assert, and
  the step prints a reminder instead.
- The other nightly jobs run inside the app itself (see
  [CLI and jobs](../reference/cli.md)).

Notes on the proxy:

- With `[proxy] caddy = false`, TLS and the public hostname are **yours**.
  Your proxy terminates HTTPS and reverse-proxies to the loopback port.
  `deploy/examples/Caddyfile` or `deploy/examples/nginx.conf` is the block to
  copy.
- In both modes the block must compress (`encode zstd gzip`), because the app
  serves no compressed responses itself.
- See [Production deployment architecture](../explanation/deployment.md).

## One-time: a database that predates the migration squash

- The squash collapsed revisions `0001`–`0028` into a single `0001`.
- So `alembic upgrade head` cannot move a database still stamped `0023`. That
  revision is no longer in the chain, and alembic stops rather than guess.
- Any instance last deployed before the squash needs one hand-run catch-up,
  once, *before* the next deploy.

Check where it stands, on the host:

```sh
podman exec -i volunteerdb-db psql -U volunteerdb -Atq volunteerdb \
  -c "select version_num from alembic_version"
```

- If that says `0001`, this section is already behind you.
- Otherwise copy the catch-up scripts to the host. Copy **both** of them if
  it says `0022`.
- `0022` is where this instance sat: `0023` reached the repository but never
  reached production. So the live database was one revision behind the chain
  when the squash happened.

At `0022`, start with the bridge. At `0023`, skip straight to the second
command:

```sh
podman exec -i volunteerdb-db psql -U volunteerdb -v ON_ERROR_STOP=1 \
  volunteerdb < catchup-0022.sql        # only if the revision is 0022
```

- `catchup-0022.sql` is revision `0023` written out as SQL: three nullable
  columns on `app_user` and one unique constraint, no data migration.
- It leaves the database stamped `0023`, which is what the next script
  expects.

Then, in both cases:

```sh
podman exec -i volunteerdb-db pg_dump -U volunteerdb volunteerdb \
  | gzip > ~/before-catchup-$(date +%F).sql.gz          # first, always
podman exec -i volunteerdb-db psql -U volunteerdb -v ON_ERROR_STOP=1 \
  volunteerdb < catchup-0023.sql
podman run --rm --network volunteerdb --env-file /etc/volunteerdb/env \
  localhost/volunteerdb:latest alembic stamp 0001
```

- Each script runs as one transaction. It refuses to start against any
  revision but its own, so a second invocation is a loud no-op, not a mess.
- Together they apply exactly what `0023`–`0028` did.
- A build of both paths on scratch databases confirmed the result: the diff
  of `pg_dump --schema-only` was empty.
- That diff covered the two constraint names that PostgreSQL would otherwise
  have derived differently, and the now-unused `pg_trgm` extension.
- Afterwards, deploy normally. `alembic upgrade head` sees `0001` and has
  nothing to do.

:::{warning}
**Deploy immediately after.** The catch-up drops columns that the *live*
image still maps: `volunteer.updated_at`, the notification stamps, the
`event_task_force` table. So between the catch-up and the deploy, the old
container answers 500 on any page that touches a volunteer. Stop
`volunteerdb-app.service` before the catch-up and let the deploy start it
again. Or run the two back to back and accept a window of a minute or so. Do
not begin if you cannot finish.
:::

## Verify

- The deploy's smoke test passed (it fails loudly otherwise).
- `https://<your domain>/login` loads and a sign-in works.
- On the host: `systemctl status volunteerdb-app volunteerdb-db` — both
  active, health checks pass.

## Roll back an upgrade

- The deploy rebuilds the image from the synced source. So a rollback of
  application code = a deploy of an older commit.
- Prefer a revert on `main`, so CI redeploys and history stays honest about
  what is live:

```sh
git revert <bad-commit> && git push origin main
```

Break-glass, if CI is unavailable:

```sh
git checkout <known-good-commit>
make deploy SITE=<your-site>
git checkout main
```

- The deploy does not downgrade migrations. If the bad release included one,
  restore the database from a [backup](backup-restore.md), or run a reviewed
  `alembic downgrade` as a one-shot container.
- Note that `0001` is now the base revision. A downgrade of it drops every
  table. So for anything below the current head, restore from backup instead.
- The managed Caddyfile rolls back with the code: the older commit's site
  file renders the older configuration.
- The hand-written file that the first managed deploy replaced is still on
  the server as `/etc/caddy/Caddyfile.bak-pre-managed-<date>`. Copy it back
  and run `systemctl reload caddy` to restore it at once. Then set
  `[proxy] caddy = false`, or the next deploy overwrites it again.
