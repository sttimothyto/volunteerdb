# Production deployment architecture

Production is one small VPS running the app and its database as two podman
containers under systemd, fronted by Caddy. Everything the repository manages
is applied by one idempotent pyinfra script — the
[deploy how-to](../how-to/deploy.md) is the operating manual and
[Stand up a new instance](../how-to/new-instance.md) is the first-time
checklist; this page is the *why*.

## One repository, many parishes

The deploy is mechanism only. Everything that distinguishes one parish's
deployment from another's is in a single declarative file,
`deploy/sites/<name>.toml` — the host, the domain, the port, whether the
deploy owns the reverse proxy, the five mail addresses, the rclone remote, the
retention windows, the five nightly times.
`deploy/siteconf.py` holds what is identical everywhere: paths, image and
network names, the container UID. `deploy/deploy.py` holds neither; it is
the running order and nothing else.

The split is what makes a second instance possible without editing Python,
and it is enforced rather than encouraged. The site loader rejects unknown
and missing keys, a test renders every template and fails if any variable is
unsupplied, and another test holds the five nightly times to their required
order, and the loader checks that TOML gave each key the type the templates
expect. That strictness earns its keep because the alternative is late: a
typo would otherwise surface as pyinfra refusing a template halfway through a
deploy, with the host half-updated, rather than as a failing test.

It also drew a useful line through the application. Anything the app needs to
know about the parish — its name, its time zone, the address it sends from —
now arrives as configuration through `/etc/volunteerdb/env`, which the deploy
generates from the same file. Nothing about a particular parish is compiled
in.

## Quadlets: containers as systemd units

Instead of a compose file or an orchestrator, the stack is three Quadlet
files in `/etc/containers/systemd/` that podman turns into ordinary systemd
services:

- `volunteerdb.network` — a named podman network; containers find each
  other by DNS name on it.
- `volunteerdb-db.container` — PostgreSQL 17 with a named volume. **No
  published port**: the database is reachable only from the podman network,
  so it simply has no internet-facing surface. Its start blocks until
  `pg_isready` succeeds, which serializes app startup correctly.
- `volunteerdb-app.container` — the app image, publishing
  `127.0.0.1:<listen_port> → 8080` (loopback only — Caddy is the sole public
  entrance), with `NoNewPrivileges`, a non-root user, a Python healthcheck,
  and `Restart=always`.

The payoff is that the whole operational surface is systemd: `systemctl
status/restart`, journald logs, start-on-boot — no new tooling for the
2 a.m. version of the operator. Dev uses compose for convenience
(`compose.yaml`); prod uses quadlets for supervision. Same images, same
env-file contract.

## The image is built on the server

The deploy syncs the source to `/opt/volunteerdb/app` and runs
`podman build` there, rather than pushing to a registry. For a
single-server deployment a registry adds cost and secrets for no benefit;
syncing source keeps the build context inspectable on the host, and the
multi-stage Containerfile (uv dependency layer cached separately from the
source layer) keeps rebuilds fast. One image serves three jobs — web
process (default `CMD`), migration runner, and admin bootstrap — run as
one-shot containers during deploy, so schema upgrades use exactly the code
and environment of the release they belong to. A separate build stage also
renders this manual into the image, so `/manual` always documents the
release that is actually running.

## Secrets manage themselves

The deploy reads `/etc/volunteerdb/env` back on every run: existing values
(storage secret, DB password, SMTP key) are reused, missing ones are
generated. There is no secrets vault to stand up, nothing secret in the
repository, and re-running the deploy is always safe. Rotation is the
explicit exception, covered in [Rotate secrets](../how-to/rotate-secrets.md).

## Nightly backups

A systemd timer (installed by the deploy; `Persistent=true`, so a night
the host slept through is made up at boot) runs a small script at the site's
`[schedule] backup_at`, in parish time: `pg_dump | gzip` out of the database container, an atomic
rename so a half-written dump never gets a dated name, then `rclone copy`
through an encrypting *crypt* remote to a Google Drive folder owned by
the parish account, keeping the site's retention windows (14 days on disk
and two years on Drive, here).
Output lands in journald (`journalctl -u volunteerdb-backup`) and any
failure emails the site's `[mail] alert_email` through the same SMTP2GO
account the app sends with, so a silently broken backup cannot rot unnoticed.

Three deliberate choices:

- **The Drive token is least-privilege.** The rclone remote uses the
  `drive.file` scope, so the credential on the server can only see files
  rclone itself created — a leaked token exposes backups, not the whole
  Drive.
- **Drive copies are encrypted; local ones are not.** The Drive leg is
  where backup exposure actually lives: a consumer cloud account,
  shareable folders, and up to two years of nightly snapshots of names,
  contact details and — via ministry membership — religious affiliation.
  So dumps pass through an rclone `crypt` remote and reach Drive as
  ciphertext, contents and filenames both. The local copies stay
  plaintext on purpose: they are root-only files on the same host as the
  live database, so encrypting them adds nothing, and plain files keep
  restores simple. The price is a custody duty — the crypt password must
  also live *off* the server (password manager + printed copy), because
  server loss is precisely the scenario where Drive backups are needed
  and `rclone.conf` is gone.
- **The rclone config is outside the deploy.** rclone rewrites the OAuth
  token inside `/root/.config/rclone/rclone.conf` on every refresh; a
  deploy that templated the file would clobber live tokens. It (and the
  crypt wrapper) is provisioned once by hand
  ([Back up and restore](../how-to/backup-restore.md)) and the deploy
  merely asserts both remotes exist.

## The reverse proxy: managed, or yours

Caddy terminates HTTPS with automatic Let's Encrypt certificates and proxies
to the site's loopback port; the app just trusts that TLS happened
(`VDB_COOKIE_SECURE=true`, `FORWARDED_ALLOW_IPS=*`). Whether the deploy owns
that Caddy is the site file's choice, `[proxy] caddy`.

With `caddy = true` the deploy owns `/etc/caddy/Caddyfile` outright — the
same whole-file, generated, do-not-edit contract as the env file and the
quadlets — and other sites the same Caddy should serve go into
`[proxy] extra`, verbatim. Three details carry the weight:

- **Validate, then install.** The file is rendered to a candidate beside the
  live one and `caddy validate`d there; only a valid candidate is installed.
  A typo in `extra` stops the deploy with the old configuration still on disk
  and still being served — never an invalid Caddyfile at rest, which Caddy
  would survive until its next restart and then not.
- **Reload, never restart.** Caddy keeps its certificate cache across a
  reload, so a deploy costs no issuance and no downtime.
- **DNS first.** The step refuses to touch Caddy until `[host] domain`
  resolves to `[host] public_ip` from the server. A name that does not yet
  resolve would send Caddy into Let's Encrypt's failed-validation backoff,
  which is measured in hours.

That mode assumes a server on its own public address, which is any VPS. With
`caddy = false` the deploy never touches Caddy or the firewall, and the proxy
is yours: a Caddy serving other sites you would rather keep by hand, an nginx
already there, a host behind NAT or a CDN. `deploy/examples/` holds the block
for either.

## What is deliberately outside the repo

- **DNS.** The A record is yours; the deploy only checks it.
- **The rclone Google Drive credentials** (OAuth token + backup-encryption
  password) — see [Nightly backups](#nightly-backups). A site that sets
  `[backup] rclone_remote = "none"` has no Drive leg at all: the nightly dump
  stays on the host, and every deploy says so.

## Known gaps (honesty section)

- **DB password rotation is manual** (`POSTGRES_*` applies only at first
  initdb) — sequence in [Rotate secrets](../how-to/rotate-secrets.md).
- **Single host, no monitoring beyond systemd/healthchecks** — acceptable
  at parish scale, worth revisiting if that changes.
