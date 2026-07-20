# Configuration

The application reads its settings from environment variables prefixed
`VDB_`, optionally loaded from a `.env` file in the working directory
(`src/volunteerdb/config.py`, pydantic-settings). Unknown variables are
ignored. Copy `.env.example` as a starting point.

## Application settings (`VDB_*`)

`VDB_DATABASE_URL`
: Async SQLAlchemy URL of the PostgreSQL database.
  Default: `postgresql+asyncpg://volunteerdb:volunteerdb@localhost:5432/volunteerdb`.

`VDB_STORAGE_SECRET`
: Secret that signs the NiceGUI session cookie. Default: empty, which
  generates an ephemeral per-boot secret (every restart signs everyone out) —
  acceptable in development only. The placeholders `dev-secret-change-me` and
  `change-me-to-a-long-random-string` are rejected the same way. **Required
  in production**; the deploy generates and persists one automatically (see
  [Rotate secrets](../how-to/rotate-secrets.md)).

`VDB_COOKIE_SECURE`
: Adds the `Secure` attribute to the session cookie. Default: `false`.
  Set `true` whenever the app is served over HTTPS (production sets it via
  the deploy template).

`VDB_HOST`
: Bind address. Default: `0.0.0.0`.

`VDB_PORT`
: Bind port. Default: `8080`. Also read by `healthcheck.py` to find the
  running app.

`VDB_RELOAD`
: Uvicorn auto-reload for development. Default: `false`.

`VDB_SMTP2GO_API_KEY`
: API key for outbound email via SMTP2GO. Default: empty, in which case
  every email (invites, OTP codes, welcome messages) is printed to the log
  instead of sent — useful in development, where the printed OTP code is how
  you complete a passwordless login.

`VDB_MAIL_FROM`
: Sender address for outbound email. Default: `no-reply@sttimothyto.org`.

`VDB_MAIL_FROM_NAME`
: Sender display name. Default: `VolunteerDB`.

`VDB_DOCS_DIR`
: Directory of built documentation HTML served at `/manual` (signed-in
  users). Default: `docs/_build/html`, resolved against the working
  directory — in development, build the docs and they appear. The container
  image bakes the manual in and sets `/app/docs-html`. If the directory is
  missing, `/manual` returns a hint instead.

## Variables read by scripts (not the app)

`VDB_ADMIN_EMAIL`, `VDB_ADMIN_PASSWORD`
: Required by `deploy/files/create_admin.py`, the idempotent admin bootstrap
  baked into the container image. See [Commands and scripts](cli.md).

`VDB_SEED_ADMIN_PASSWORD`
: Password for the demo admin account created by `scripts/seed.py`.
  Default: `changeme`.

## Database container variables (`POSTGRES_*`)

Consumed by the PostgreSQL 17 container, not by VolunteerDB itself
(`compose.yaml` in development, `/etc/volunteerdb/db.env` in production):

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
: All default to `volunteerdb` in development. Under podman-compose the
  `.env` file is **mandatory**: `${VAR:-default}` fallbacks are not applied,
  so a missing `.env` bakes the literal placeholder into the database volume.

:::{warning}
`POSTGRES_*` values are applied only when the data volume is first
initialized (`initdb`). Changing `POSTGRES_PASSWORD` later does **not**
change the database password — see
[Rotate secrets](../how-to/rotate-secrets.md).
:::

## Configuration stored in the database

Capacity settings (role multipliers and color bands) are not environment
variables: they live in the `app_setting` table under the key `"capacity"`
and are edited on the `/admin/capacity` page or via
`PUT /api/capacity/config`. See [The capacity model](../explanation/capacity.md)
and the {ref}`schema reference <app_setting>`.
