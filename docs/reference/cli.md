# Commands and scripts

- All commands run from the repository root with
  [uv](https://docs.astral.sh/uv/).

## `volunteerdb` — run the application

```sh
uv run volunteerdb
```

- The console script (`volunteerdb.main:run`).
  `uv run python -m volunteerdb.main` is equivalent.
- It starts the combined NiceGUI + FastAPI process on `VDB_HOST:VDB_PORT`
  (default `0.0.0.0:8080`).
- It reads all [`VDB_*` settings](configuration.md).
- With `VDB_RELOAD=true`, the server restarts on source changes
  (development).
- In the container image this is the default command
  (`CMD ["volunteerdb"]`).

## `alembic` — database migrations

```sh
uv run alembic upgrade head      # apply pending migrations
uv run alembic revision -m "..." # start a new migration
uv run alembic downgrade -1      # step back one revision
```

- Targets the database from `VDB_DATABASE_URL`. `migrations/env.py`
  overrides the `sqlalchemy.url` in `alembic.ini`.
- Revision files live in `migrations/versions/`.
- See [Write a database migration](../how-to/write-a-migration.md). Column
  changes on versioned tables have a special recipe.

## `scripts/seed.py` — demo data

```sh
uv run python scripts/seed.py
```

- Seeds a fresh database with a whole demo parish, shaped to exercise every
  feature:
  - 34 teams, 3 levels deep
  - ~150 volunteers (some archived, some with no email, one household that
    shares an address)
  - ~290 memberships
  - ended and rejoined spells, and mid-spell promotions, for the timeline
    chart
  - 5 custom fields
  - placeholder headshots
  - ~55 events either side of today, with rosters, RSVPs, substitutions and
    attendance overrides
  - one proposal in every state (nominating, voting, awaiting decision,
    appointed, cancelled, re-opened)
  - 6 published ministry pages
- A team named **Clergy** is filled. That is all it takes to be the
  {term}`clergy team`, so proposals opened on the demo data get a realistic
  voting roll.
- The script refuses to run if any volunteers exist.
- Everything is deterministic: one fixed RNG seed, dates relative to today.
  So a reseed reproduces the same parish.

The seed creates these logins, **all on the password `demo`**:

- one for the administrator
- a few shaped accounts (below)
- one for each ministry leader

That is 33 logins in all at present. `make seed` prints the full list. The
notable ones:

| Email | Password | Role |
|---|---|---|
| `admin@example.org` | `$VDB_SEED_ADMIN_PASSWORD` (default `demo`) | admin |
| `helen.park@example.org` | `demo` | a second admin, linked to a volunteer |
| `maria.alvarez@example.org` | `demo` | ministry leader (two teams, red workload) |
| `felix.garcia@example.org` | `demo` | plain member |
| `dominic.ferraro@example.org` | `demo` | clergy — sits on every voting roll |
| `claire.dubois@example.org` | — | invite not yet redeemed |
| `irene.p@example.org` | — | no password: signs in with an emailed code |
| `george.ivanov@example.org` | `demo` | deactivated: sign-in is refused |

```{warning}
`demo` is 4 characters, so it does **not** clear the
[password policy](../explanation/auth.md#what-a-password-has-to-be).
`users.create` would refuse it. The seed writes the argon2 hash onto the
account itself instead. That bypass exists only in this script. The seed
widened no path that a user can reach. It is the reason to seed nothing but
a throwaway development database.
```

## `scripts/google_authorize.py` — the parish Google token

```sh
python scripts/google_authorize.py CLIENT_ID CLIENT_SECRET [PORT]
```

- Run it once, on a machine with a browser. Never run it on the server.
- In the browser, sign in as the parish Google account.
- Use the OAuth client that the rclone backup remote already uses.
  `drive.file` and `calendar.app.created` are scoped per client per file,
  so only that client can reach the sheets and the calendar it created.
- The script opens the consent screen and catches the redirect on loopback
  port `PORT` (default 8765).
- A *Web application* client accepts only its registered ports. rclone
  registers 53682.
- The script exchanges the code and prints `VDB_SHEETS_REFRESH_TOKEN`.
- It asks for 4 scopes at once: `spreadsheets`, `drive.file`,
  `calendar.app.created`, `calendar.acls`. So one token serves both the
  [roster sheets](../how-to/roster-spreadsheets.md) and the
  [parish calendar](../how-to/google-calendar-sync.md).
- Stdlib only.

## `scripts/share_roster_sheets.py` — re-share existing sheets

```sh
uv run python scripts/share_roster_sheets.py --dry-run
uv run python scripts/share_roster_sheets.py
```

- A one-shot for an instance whose roster sheets the retired rclone sync
  created. That sync shared them per leader.
- The script shares every sheet in `team_sheet` as "anyone with the link can
  edit", which is what the sync now reads.
- Run it once after `VDB_SHEETS_*` is set and before the first nightly run.
- Until then the sync gets a sign-in page instead of a roster and reports
  "the spreadsheet is not shared".
- It is idempotent, and it leaves the old per-leader grants alone.
- It requires the OAuth client that created the sheets.
- A fresh instance never needs it.

## `scripts/bench.py` — query benchmark

```sh
uv run python scripts/bench.py setup --scale 500
uv run python scripts/bench.py run --json bench-results/base-500.json [--explain] [--only NAME] [--runs N]
uv run python scripts/bench.py compare bench-results/base-500.json bench-results/after-500.json
```

- Runs against a disposable `volunteerdb_bench` database, never the
  development or test one.
- `setup` drops, recreates, migrates and seeds it with a deterministic parish
  of `--scale` volunteers. The default is 500; 5000 is the growth-headroom
  scale.
- `run` times each hot-query pattern (median and p90 after warm-up, 15 runs
  by default). It also counts the SQL statements each pattern issues, which
  is the N+1 headline.
- `--explain` prints `EXPLAIN (ANALYZE, BUFFERS)` for every `SELECT`,
  exactly as the app issued it.
- `compare` diffs two run files.
- `bench-results/` is gitignored.

## `volunteerdb.admin_bootstrap` — admin bootstrap

```sh
VDB_ADMIN_EMAIL=... VDB_ADMIN_PASSWORD=... uv run python -m volunteerdb.admin_bootstrap
```

- Idempotent: it creates the admin account if it is missing, and otherwise
  leaves it untouched.
- It is part of the package, so any image built from this repository can
  run it.
- The deploy runs it as a one-shot container when you pass
  `VDB_ADMIN_PASSWORD`.
- It requires `VDB_ADMIN_EMAIL` and `VDB_ADMIN_PASSWORD`.
- This is the **only** way into a brand-new instance. Pass
  `VDB_ADMIN_PASSWORD` on the first deploy, or there is no account to sign
  in with.
- The script checks `VDB_ADMIN_PASSWORD` against the
  [password policy](../explanation/auth.md#what-a-password-has-to-be) before
  it touches the database. If the password is too short or too well-known,
  the script exits 2 with the reason on stderr. It has created nothing at
  that point.

## `volunteerdb.jobs` — scheduled one-shot jobs

```sh
python -m volunteerdb.jobs.roster_sync
python -m volunteerdb.jobs.fetch_pages
python -m volunteerdb.jobs.proposal_digest [--today YYYY-MM-DD]
python -m volunteerdb.jobs.event_reminders [--today YYYY-MM-DD]
python -m volunteerdb.jobs.calendar_sync
python -m volunteerdb.jobs.task_force_cleanup
```

- `fetch_pages` refreshes every team home page from its public Google Doc
  (see [Publish a team home page](../how-to/team-home-pages.md)). Each team
  fetches in its own transaction, so one bad doc cannot block the rest.
- `roster_sync` reconciles every team's roster with its Google Sheet (see
  [Sync team rosters with Google Sheets](../how-to/roster-spreadsheets.md)).
  - Each team syncs independently, so one unshared sheet cannot stop the
    rest.
  - Unconfigured, it exits 0 with "not configured".
  - It sends no mail. A pass over a messy sheet can redirect dozens of
    addresses at once. Mail to each of the old mailboxes, usually the broken
    ones under repair, spent a noticeable slice of a 200-message day for
    nothing.
  - The redirects still land in `volunteer_history`.
- `proposal_digest` emails each proposal voter one nightly digest of what
  needs their input.
- `event_reminders` emails each volunteer their event notices: a manager
  scheduled them, they serve this week, they serve tomorrow. The reminder
  stages honour the per-sign-up preferences.
- Both digests are per-person idempotent, through notification stamps. A
  failed send retries the next night.
- Both take `--today` for manual runs and tests.
- All of the jobs need `VDB_DATABASE_URL`.
- `calendar_sync` reconciles events onto the public parish Google Calendar
  (see [Publish events to a Google Calendar](../how-to/google-calendar-sync.md)).
  Unconfigured, it exits 0 with "not configured", so it is always safe to
  run.
- `task_force_cleanup` dismantles the auto-created task-force team of every
  finished or cancelled event (see
  [Events and scheduling](../explanation/events.md)).

All of these run **inside the app process**:

- `volunteerdb.scheduler` fires the nightly four at their parish-local times:
  `VDB_ROSTER_SYNC_AT` 02:30, `VDB_FETCH_PAGES_AT` 03:00,
  `VDB_PROPOSAL_DIGEST_AT` 03:30, `VDB_EVENT_REMINDERS_AT` 04:00.
- It runs `calendar_sync` every 30 minutes and `task_force_cleanup` every
  hour.
- It records completion in the `job_run` table, so a restart or redeploy
  cannot skip a night.
- It retries a failed nightly job every 30 minutes, up to 3 attempts per
  day.
- It emails `VDB_ALERT_EMAIL` at most once per job per parish day. A further
  failure on the same day logs but does not mail.
- An interval job retries on its own cadence instead, and the 3-attempt
  limit does not apply to it.
- Their output lands in the app journal (`journalctl -u volunteerdb-app`,
  grep `scheduler`).
- The `python -m` forms above stay for manual runs. In production:
  `podman run --rm --network volunteerdb --env-file /etc/volunteerdb/env
  localhost/volunteerdb:latest python -m volunteerdb.jobs.<name>`.
- Both the scheduler and the CLI take an advisory lock. So a manual run and
  the scheduler never run the same job at the same time.
- The 02:00 backup remains the one host-side systemd timer.
- The roster sync used to be the other, wrapped in rclone transfers. When
  the sync started to read a link-shared sheet, the host leg became
  unnecessary.

## `healthcheck.py` — container health probe

```sh
python /app/healthcheck.py
```

- Exits 0 if and only if `http://127.0.0.1:$VDB_PORT/login` responds.
- The app quadlet's `HealthCmd` references it. You do not normally run it
  by hand.

## `pyinfra` — production deploy

```sh
make deploy-dry SITE=<site>   # preview
make deploy SITE=<site>       # apply
```

- Runs `VDB_SITE=<site> uvx pyinfra==<pin> deploy/inventory.py deploy/deploy.py`
  with `--dry` or `-y`. The Makefile holds the pin.
- Deploys or updates the whole containerized stack on the server, and the
  reverse proxy when the site file asks for it.
- Optional environment: `VDB_ADMIN_PASSWORD` (runs the admin bootstrap),
  `VDB_SMTP2GO_API_KEY` (sets or rotates the mail key).
- See [Deploy and upgrade production](../how-to/deploy.md).

## `pytest` — test suite

```sh
uv run pytest
```

- The development database container must be up.
- See [Run the test suite](../how-to/run-tests.md).

## Documentation build

```sh
uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
```

- Sphinx and its extensions come from PyPI, pinned in the `docs` dependency
  group in `pyproject.toml`.
- The container image builds the same manual in its own stage, so `/manual`
  always documents the release that runs.

## `make` — developer tasks

- Thin wrappers over the commands above. `make` alone lists them.
- Override the tools with variables, for example
  `make db COMPOSE=docker-compose`.

| Target | Does |
|---|---|
| `make db` | start PostgreSQL and wait until it accepts queries (creates `.env` with a generated secret if missing) |
| `make migrate` | `alembic upgrade head` |
| `make seed` | migrate, then load the demo parish and print its logins |
| `make dev` | serve `http://localhost:8080`, restarting on source changes |
| `make serve` | serve without auto-reload |
| `make test ARGS="…"` | the test suite, `ARGS` passed to pytest |
| `make lint` / `make format` | ruff check, or reformat in place |
| `make docs` | build this manual into `docs/_build/html` |
| `make fresh` | wipe the database volume, then migrate and seed from scratch |
| `make down` / `make clean` | stop the containers, keeping or deleting the data volume |
| `make deploy-dry SITE=…` / `make deploy SITE=…` | preview or apply the production deploy |
