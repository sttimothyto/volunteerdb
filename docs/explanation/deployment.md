# Production deployment architecture

Production is one small VPS (`sttimothyto-prod`) running the app and its
database as two podman containers under systemd, fronted by Caddy at
<https://vdb.sttimothyto.org>. Everything the repository manages is applied
by one idempotent pyinfra script — the [deploy how-to](../how-to/deploy.md)
is the operating manual; this page is the *why*.

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
  `127.0.0.1:8090 → 8080` (loopback only — Caddy is the sole public
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

## What is deliberately outside the repo

- **TLS and the public name.** Caddy (configured in `/etc/caddy/Caddyfile`
  on the host) terminates HTTPS with automatic Let's Encrypt certificates
  and proxies to `127.0.0.1:8090`; DNS points `vdb.sttimothyto.org` at the
  server. The app just trusts that TLS happened (`VDB_COOKIE_SECURE=true`).
- **The old native install.** The deploy still carries a guarded one-time
  cutover/rollback path from the pre-container deployment; it is inert once
  the host PostgreSQL is retired.

## Known gaps (honesty section)

- **No scheduled backups** — dumps are manual;
  [Back up and restore](../how-to/backup-restore.md) sketches the cron fix.
- **DB password rotation is manual** (`POSTGRES_*` applies only at first
  initdb) — sequence in [Rotate secrets](../how-to/rotate-secrets.md).
- **Single host, no monitoring beyond systemd/healthchecks** — acceptable
  at parish scale, worth revisiting if that changes.
