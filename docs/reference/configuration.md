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

`FORWARDED_ALLOW_IPS`
: Uvicorn's trusted-proxy list (no `VDB_` prefix). Default: `127.0.0.1`.
  When the app runs behind a TLS-terminating reverse proxy, the proxy's
  `X-Forwarded-Proto` header is only honored if the connection comes from a
  listed address — otherwise absolute URLs the app generates (invite and
  sign-in links in emails, the admin backup-link dialog) come out `http://`.
  In the production container, Caddy's connections arrive from the podman
  gateway address, so the deploy template sets `*`; that is safe because the
  container port is published on the host loopback only.

`VDB_HOST`
: Bind address. Default: `0.0.0.0`.

`VDB_PORT`
: Bind port. Default: `8080`. Also read by `healthcheck.py` to find the
  running app.

`VDB_RELOAD`
: Uvicorn auto-reload for development. Default: `false`.

`VDB_LOG_LEVEL`
: Minimum log verbosity. Default: `AUDIT`, which logs every database write
  (who, when, from where, and the old → new values), authentication events,
  commit/rollback markers, and problems. `INFO` adds one line per database
  read and per HTTP request; `DEBUG` adds query parameters and static-asset
  requests; `WARNING` keeps problems only. See
  [Read the audit log](../how-to/audit-logs.md).

`VDB_LOG_FILE`
: Path of an additional rotating log file (10 MB per file, 6 files kept).
  Default: empty — logs go to stderr only, which journald captures in
  production. In the container this path must point into a writable volume
  (only `/app/.nicegui` is writable by default).

`VDB_SMTP2GO_API_KEY`
: API key for outbound email via SMTP2GO. Default: empty, in which case
  nothing is sent and each message is logged instead. Pair it with
  `VDB_DEBUG_MAIL` in development to read the printed OTP code and complete a
  passwordless login.

`VDB_DEBUG_MAIL`
: With no API key, print the whole message body rather than a one-line
  warning. Default: `false`, and **implied by `VDB_RELOAD`** — so `make dev`
  shows you the OTP code without setting anything. For development only: the
  bodies carry one-time sign-in codes, invite links and address-change links,
  so an instance that merely forgot `VDB_SMTP2GO_API_KEY` would otherwise
  write every credential it issues into the process log, where the app's own
  redaction cannot reach it.

`VDB_ORG_NAME`
: The organisation this instance serves, e.g. `St. Timothy's`. It appears in
  outbound mail ("Your VolunteerDB account at …"), and its name and mail
  domain become context-specific terms in the
  [password policy](../explanation/auth.md) — so a parish's own name cannot
  be used as a password. Default: empty, in which case the mail copy drops
  the clause rather than naming a placeholder. Production sets it from the
  site file, which also passes it to the docs build so the manual at
  `/manual` carries the same name.

`VDB_MAIL_FROM`
: Sender address for outbound email. Default: `no-reply@example.invalid` —
  a reserved domain, so an instance that never set it is obvious rather than
  quietly sending as someone else. **The address must be on a domain your
  mail provider is authorised to send for**, or delivery fails. The
  `drive-sync@…` account that owns Drive-sync history entries is derived from
  this domain.

`VDB_MAIL_FROM_NAME`
: Sender display name. Default: `VolunteerDB`.

`VDB_PUBLIC_BASE_URL`
: Absolute origin (e.g. `https://vdb.example.org`) used for links in
  emails sent by nightly jobs, which have no live request to derive one from —
  today, the events link in the nightly reminder digest. Default: empty, in
  which case those emails simply omit the link (mail sent from the GUI
  keeps deriving links from the request either way). The deploy writes the
  production domain and reads it back on later runs, like
  `VDB_TEMPLATE_SHEET_URL`.

`VDB_SCHEDULER_ENABLED`
: The in-app scheduler that runs the nightly jobs (`fetch_pages`,
  `roster_sync`, `proposal_digest`, `event_reminders`) inside the server
  process. Default:
  `true`. Always off under `VDB_RELOAD=true` regardless — dev reload
  restarts the process on every save, which would re-fire startup hooks.
  See [CLI and jobs](cli.md).

`VDB_ALERT_EMAIL`
: Where the scheduler emails when a nightly job fails. Default: empty —
  failures only log at ERROR. Production sets the same address the host
  backup/sync wrappers alert. **One alert per job per parish day**, however
  many times it fails: a nightly job retries every 30 minutes up to 3 times,
  and an interval job keeps retrying on its own cadence, but the second and
  third messages say nothing the first did not and the instance sends on a
  1,000-a-month allowance. The retries still happen and still log.

`VDB_SUPPORT_CONTACT`
: Who the mail-allowance banner tells an admin to contact — whoever set the
  instance up and can raise the plan or cut the sending. Default: empty,
  falling back to `VDB_ALERT_EMAIL`; with both empty the banner still
  appears and simply names nobody. See
  [Architecture](../explanation/architecture.md).

`VDB_ROSTER_SYNC_AT`, `VDB_FETCH_PAGES_AT`, `VDB_PROPOSAL_DIGEST_AT`, `VDB_EVENT_REMINDERS_AT`
: Parish-local (`VDB_TIMEZONE`) times the in-app scheduler runs each
  nightly job. Defaults: `02:30`, `03:00`, `03:30`, `04:00` — kept clear of
  02:00, when the host's backup timer runs. Setting one a couple of minutes
  ahead is the way to watch a job fire in dev.

`VDB_INVITE_TTL_HOURS`
: How long an invite link stays redeemable — and since re-inviting is how a
  password is reset, how long a reset link lives. Default: `168` (7 days) — a
  deliberate deviation from the 24-hour ceiling NIST SP 800-63B §4.2.1.2 puts
  on a recovery code sent to an email address, sized for a parish where
  invitees read email weekly. Lower it if that trade-off changes. Expiry is
  never a lockout: the account still signs in with an emailed code and can
  set a password from **/account**. See
  [Authentication design](../explanation/auth.md).

`VDB_TIMEZONE`
: IANA zone the parish lives in. Default: `America/Toronto`. Date-typed
  values like election deadlines mean "through the end of that day *here*":
  the phase of an open proposal is computed against today's date in this
  zone, not against the container's clock (UTC in production). Event
  reminder windows and the weekly repeat helper use the same zone.

`VDB_TEMPLATE_SHEET_URL`
: URL of the decorated roster-template Google Sheet, shared read-only to
  anyone with the link. Default: empty, in which case a team's **Roster
  spreadsheet** section offers a plain CSV download instead of linking to the
  sheet. The deploy writes it and reads it back on later runs, like
  `VDB_PUBLIC_BASE_URL`; set it once after the template sheet exists. See
  [Sync team rosters with Google Sheets](../how-to/roster-spreadsheets.md).

`VDB_SHEETS_CLIENT_ID`, `VDB_SHEETS_CLIENT_SECRET`, `VDB_SHEETS_REFRESH_TOKEN`, `VDB_SHEETS_FOLDER_ID`
: Roster spreadsheet sync: the OAuth client and refresh token authorised as
  the parish Google account (`scripts/sheets_authorize.py`), and the id of the
  Drive folder new roster sheets are created in. With all four set, the in-app
  scheduler syncs every team's roster with its sheet nightly and a team page
  can sync one on demand. Defaults: empty — the job exits "not configured" and
  the team page says so. Only the *write* leg needs these: reading a sheet
  shared "anyone with the link can edit" takes no credentials at all. The
  deploy writes them and reads them back on later runs, like
  `VDB_TEMPLATE_SHEET_URL`; set them once after the one-time authorization.
  See [Sync team rosters with Google Sheets](../how-to/roster-spreadsheets.md).

`VDB_GCAL_CLIENT_ID`, `VDB_GCAL_CLIENT_SECRET`, `VDB_GCAL_REFRESH_TOKEN`, `VDB_GCAL_CALENDAR_ID`
: Google Calendar sync: the OAuth client and refresh token authorised as
  the parish Google account, and the id of the public calendar it owns.
  With all four set, the in-app scheduler reconciles upcoming events onto
  that calendar every 30 minutes (one-way, VolunteerDB → Google; an edit
  reaches the calendar within half an hour) and the **/events** page embeds
  it. Defaults: empty — the sync exits "not configured" and nothing embeds.
  The deploy writes them and reads them back on later runs, like
  `VDB_TEMPLATE_SHEET_URL`; set them once after the one-time authorization.
  See [Publish events to a Google Calendar](../how-to/google-calendar-sync.md).

`VDB_DOCS_DIR`
: Directory of built documentation HTML served at `/manual` (signed-in
  users). Default: `docs/_build/html`, resolved against the working
  directory — in development, build the docs and they appear. The container
  image bakes the manual in and sets `/app/docs-html`. If the directory is
  missing, `/manual` returns a hint instead.

## Variables read by scripts (not the app)

`VDB_ADMIN_EMAIL`, `VDB_ADMIN_PASSWORD`
: Required by `python -m volunteerdb.admin_bootstrap`, the idempotent admin
  bootstrap. See [Commands and scripts](cli.md).

`VDB_DB_PASSWORD`
: The PostgreSQL role password, written into `/etc/volunteerdb/env` by the
  deploy and **ignored by the application** — which reads its credentials from
  `VDB_DATABASE_URL`. It exists so the deploy can read the password back on
  the next run and keep the role, the connection URL, and
  `/etc/volunteerdb/db.env` in agreement. Generated once, then reused. See
  [Rotate secrets](../how-to/rotate-secrets.md).

`VDB_SEED_ADMIN_PASSWORD`
: Password for the demo admin account created by `scripts/seed.py`.
  Default: `demo`, the same password every other seeded account gets. The
  seed hashes it directly, so — unlike every other way into the database —
  it is **not** held to the password policy; set this to something real if
  the demo instance is going to be reachable by anyone else.

`VDB_CONTACT_EMAIL`
: Address in this manual's page footer ("See something inappropriate? Report
  it to …"), read by `docs/conf.py` **when the docs are built**. Default:
  `admin@example.invalid`. Setting it on the running app does nothing — the
  container image bakes the built HTML in, so change it and rebuild. Point it
  at a monitored human mailbox; unlike `VDB_MAIL_FROM`, replies are the point.

## Database container variables (`POSTGRES_*`)

Consumed by the PostgreSQL 17 container, not by VolunteerDB itself
(`compose.yaml` in development, `/etc/volunteerdb/db.env` in production):

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
: All default to `volunteerdb` in development.

:::{warning}
`POSTGRES_PASSWORD` must be **actually set** — in `.env` or the environment —
before you first start the db container. podman-compose (checked on 1.3.0)
does not apply the `${VAR:-default}` fallback when the variable name matches
the key it is assigned to, as in
`POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-volunteerdb}`; it writes the literal
string `${POSTGRES_PASSWORD:-volunteerdb}` into the container instead, baking
that placeholder into the data volume as the real password. An empty `.env`,
or one without this key, does not help — only a set value does.

`.env.example` sets it, so a copied `.env` is enough, and `make db` creates
that file before starting anything. If you started the container by hand
without one, reset it: `make clean` (or `podman compose down -v`) and start
again. A fallback whose variable name *differs* from the key interpolates
normally, so this affects only the `POSTGRES_*` entries.
:::

:::{warning}
`POSTGRES_*` values are applied only when the data volume is first
initialized (`initdb`). Changing `POSTGRES_PASSWORD` later does **not**
change the database password — see
[Rotate secrets](../how-to/rotate-secrets.md).
:::

## Configuration stored in the database

Workload settings (role multipliers and color bands) are not environment
variables: they live in the `app_setting` table under the key `"workload"`
and are edited on the `/admin/workload` page or via
`PUT /api/workload/config`. See [The workload model](../explanation/workload.md)
and the {ref}`schema reference <app_setting>`.

## The clergy team

The clergy team is not configured at all: it is whichever team is named
**Clergy** when a proposal's voting roll is built. It is the only team
whose members vote on every proposal; all other voting members are drawn
from the team whose seat is being filled.

Because the name *is* the standing, there is nothing to set and nothing to
keep in sync:

- Create a team named **Clergy** under **Teams** and its members begin
  joining new rolls.
- Rename it — or delete it — to retire the standing. Both are ordinary
  team edits; no setting has to be cleared first.
- With no team by that name, rolls are simply the target team's own
  leader, second-in-command, and core members.

Changing the name reaches only *future* rolls. An open proposal's roll is
already materialised as `proposal_voter` rows and is unaffected, so no
election's electorate shifts underneath it.

:::{note}
Team names are unique per parent, not globally, so a *sub-team* also named
**Clergy** would likewise join every roll. Keep the parish to one.
:::
