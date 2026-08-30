# Production deployment architecture

Production is one small VPS. It runs the app and its database as two podman
containers under systemd, with Caddy in front. One idempotent pyinfra script
applies everything the repository manages. The
[deploy how-to](../how-to/deploy.md) is the operating manual, and
[Stand up a new instance](../how-to/new-instance.md) is the first-time
checklist. This page is the *why*.

## One repository, many parishes

The deploy is mechanism only. Everything that distinguishes one parish's
deployment from another's is in a single declarative file,
`deploy/sites/<name>.toml`. That file holds the host, the domain, the port,
and whether the deploy owns the reverse proxy. It also holds the `[mail]`
block (4 addresses and a sender name), the rclone remote, the retention
windows, and the 5 nightly times. `deploy/siteconf.py` holds what is
identical everywhere: paths, image and network names, the container UID.
`deploy/deploy.py` holds neither; it is the order of the steps and nothing
else.

The split is what makes a second instance possible without an edit to
Python, and the tests enforce it rather than encourage it. The site loader
rejects unknown and missing keys, and checks that TOML gave each key the type
the templates expect. A test renders every template and fails if any
variable is unsupplied. Another test holds the 5 nightly times to their
required order. That strictness earns its keep because the alternative is
late. A typo would otherwise surface halfway through a deploy, when pyinfra
refuses a template and the host is half-updated, and not as a failed test.

It also drew a useful line through the application. Anything the app needs
to know about the parish now arrives as configuration through
`/etc/volunteerdb/env`. That covers its name, its time zone, and the address
it sends from. The deploy generates that file from the same site file. The build compiles
nothing about a particular parish into the image.

## Quadlets: containers as systemd units

Instead of a compose file or an orchestrator, the stack is 3 Quadlet files
in `/etc/containers/systemd/`. Podman turns them into ordinary systemd
services:

- `volunteerdb.network` — a named podman network; containers find each
  other by DNS name on it.
- `volunteerdb-db.container` — PostgreSQL 17 with a named volume. **No
  published port**: only the podman network can reach the database, so it
  has no internet-facing surface. Its start blocks until `pg_isready`
  succeeds, which puts the app start after the database correctly.
- `volunteerdb-app.container` — the app image. It publishes
  `127.0.0.1:<listen_port> → 8080` (loopback only; Caddy is the sole public
  entrance). It runs with `NoNewPrivileges`, a non-root user, a Python
  healthcheck, and `Restart=always`.

The payoff is that the whole operational surface is systemd: `systemctl
status/restart`, journald logs, start-on-boot. There is no new tool for the
2 a.m. version of the operator. Dev uses compose for convenience
(`compose.yaml`); prod uses quadlets for supervision. Same images, same
env-file contract.

## The image is built on the server

The deploy syncs the source to `/opt/volunteerdb/app` and runs
`podman build` there, rather than push to a registry. For a single-server
deployment a registry adds cost and secrets for no benefit. A source sync
keeps the build context inspectable on the host. The multi-stage
Containerfile caches the uv dependency layer separately from the source
layer, which keeps rebuilds fast.

One image serves 3 jobs — the web process (default `CMD`), the migration
runner, and the admin bootstrap. The deploy runs the last two as one-shot
containers, so schema upgrades use exactly the code and environment of the
release they belong to. A separate build stage also renders this manual into
the image, so `/manual` always documents the release that actually runs.

## Secrets manage themselves

The deploy reads `/etc/volunteerdb/env` back on every run. It reuses the
values that exist (storage secret, DB password, SMTP key) and generates the
ones that are missing. There is no secrets vault to stand up, nothing secret
in the repository, and a repeat of the deploy is always safe. Rotation is
the explicit exception; [Rotate secrets](../how-to/rotate-secrets.md) covers
it.

## Nightly backups

The deploy installs a systemd timer with `Persistent=true`: if the host
slept through a night, the backup runs at the next boot. The timer runs a
small script at the site's `[schedule] backup_at`, in parish time. The
script runs `pg_dump | gzip` out of the database container, then an atomic
rename, so a half-written dump never gets a dated name. Then `rclone copy`
sends the dump through an rclone *crypt* remote, which encrypts it, to a
Google Drive folder that the parish account owns. The script keeps the
site's retention windows (14 days on disk and 2 years on Drive, here).

Output lands in journald (`journalctl -u volunteerdb-backup`). Any failure
emails the site's `[mail] alert_email` through the same SMTP2GO account the
app sends with, so a silently broken backup cannot rot unnoticed.

Three deliberate choices:

- **The Drive token is least-privilege.** The rclone remote uses the
  `drive.file` scope, so the credential on the server can only see files
  rclone itself created. A leaked token exposes backups, not the whole
  Drive.
- **Drive copies are ciphertext; local ones are plaintext.** The Drive leg
  is where backup exposure actually lives: a consumer cloud account,
  shareable folders, and up to 2 years of nightly snapshots. Those snapshots
  hold names, contact details and, through ministry membership, religious
  affiliation. So dumps pass through an rclone `crypt` remote and reach
  Drive as ciphertext, contents and filenames both.
  - The local copies stay plaintext on purpose. They are root-only files on
    the same host as the live database, so encryption adds nothing, and
    plain files keep restores simple.
  - The price is a custody duty. The crypt password must also live *off*
    the server (password manager + printed copy). Server loss is precisely
    the scenario where you need the Drive backups and `rclone.conf` is
    gone.
- **The rclone config is outside the deploy.** rclone rewrites the OAuth
  token inside `/root/.config/rclone/rclone.conf` on every refresh. A deploy
  that templated the file would clobber live tokens. So you provision it
  (and the crypt wrapper) once by hand
  ([Back up and restore](../how-to/backup-restore.md)), and the deploy
  merely asserts that both remotes exist.

## The reverse proxy: managed, or yours

Caddy terminates HTTPS with automatic Let's Encrypt certificates and proxies
to the site's loopback port. The app just trusts that TLS happened
(`VDB_COOKIE_SECURE=true`, `FORWARDED_ALLOW_IPS=*`). Whether the deploy owns
that Caddy is the site file's choice, `[proxy] caddy`.

With `caddy = true` the deploy owns `/etc/caddy/Caddyfile` outright. It is
the same whole-file, generated, do-not-edit contract as the env file and the
quadlets. Other sites that the same Caddy must serve go into
`[proxy] extra`, verbatim. Three details carry the weight:

- **Validate, then install.** The deploy renders the file to a candidate
  beside the live one and runs `caddy validate` on it there. It installs
  only a valid candidate. A typo in `extra` stops the deploy with the old
  configuration still on disk and still in service. There is never an
  invalid Caddyfile at rest, which Caddy would survive until its next
  restart and then not.
- **Reload, never restart.** Caddy keeps its certificate cache across a
  reload, so a deploy costs no issuance and no downtime.
- **DNS first.** The step refuses to touch Caddy until `[host] domain`
  resolves to `[host] public_ip` from the server. A name that does not yet
  resolve would send Caddy into Let's Encrypt's failed-validation backoff,
  which lasts hours.

That mode assumes a server on its own public address, which is any VPS. With
`caddy = false` the deploy never touches Caddy or the firewall, and the proxy
is yours. That can be a Caddy that serves other sites you would rather keep
by hand. It can be an nginx already there, or a host behind NAT or a CDN.
`deploy/examples/` holds the block for either.

## What is deliberately outside the repo

- **DNS.** The A record is yours; the deploy only checks it.
- **The rclone Google Drive credentials** (OAuth token + backup-encryption
  password) — see [Nightly backups](#nightly-backups). A site that sets
  `[backup] rclone_remote = "none"` has no Drive leg at all. The nightly
  dump stays on the host, and every deploy says so.

## Known gaps (honesty section)

- **DB password rotation is manual** (`POSTGRES_*` applies only at first
  initdb). The sequence is in [Rotate secrets](../how-to/rotate-secrets.md).
- **Single host, no monitoring beyond systemd/healthchecks** — acceptable
  at parish scale, worth a second look if that changes.
