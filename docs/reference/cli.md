# Commands and scripts

All commands run from the repository root with [uv](https://docs.astral.sh/uv/).

## `volunteerdb` — run the application

```sh
uv run volunteerdb
```

Console script (`volunteerdb.main:run`); equivalently
`uv run python -m volunteerdb.main`. Starts the combined NiceGUI + FastAPI
process on `VDB_HOST:VDB_PORT` (default `0.0.0.0:8080`). Reads all
[`VDB_*` settings](configuration.md). With `VDB_RELOAD=true` the server
restarts on source changes (development).

In the container image this is the default command (`CMD ["volunteerdb"]`).

## `alembic` — database migrations

```sh
uv run alembic upgrade head      # apply pending migrations
uv run alembic revision -m "..." # start a new migration
uv run alembic downgrade -1      # step back one revision
```

Targets the database from `VDB_DATABASE_URL` (the `sqlalchemy.url` in
`alembic.ini` is overridden by `migrations/env.py`). Revision files live in
`migrations/versions/`. See [Write a database migration](../how-to/write-a-migration.md)
— column changes on versioned tables have a special recipe.

## `scripts/seed.py` — demo data

```sh
uv run python scripts/seed.py
```

Seeds a fresh database with demo data shaped to exercise every feature:
17 teams (with sub-teams), 33 volunteers, weighted teams, two custom fields,
ended/rejoined membership spells and a mid-spell promotion for the timeline
chart. A team named **Clergy** is filled, which is all it takes to be the
{term}`clergy team`, so proposals opened on the demo data get a realistic
voting roll. Refuses to run if any volunteers exist.

Environment: `VDB_SEED_ADMIN_PASSWORD` (default `demo-parish-admin`); it
is held to the same password policy as any other account.

Created logins:

| Email | Password | Role |
|---|---|---|
| `admin@sttimothy.example` | `$VDB_SEED_ADMIN_PASSWORD` | admin |
| `maria.alvarez@example.org` | `parish-demo-login` | ministry leader (two teams) |
| `felix.garcia@example.org` | `parish-demo-login` | plain member |
| `dominic.ferraro@example.org` | `parish-demo-login` | clergy — sits on every voting roll |

## `deploy/files/create_admin.py` — admin bootstrap

```sh
VDB_ADMIN_EMAIL=... VDB_ADMIN_PASSWORD=... python create_admin.py
```

Idempotent: creates the admin account if missing, otherwise leaves it
untouched. Baked into the container image at `/app/create_admin.py`; the
deploy runs it as a one-shot container when `VDB_ADMIN_PASSWORD` is passed.
Requires `VDB_ADMIN_EMAIL` and `VDB_ADMIN_PASSWORD`.

`VDB_ADMIN_PASSWORD` is checked against the
[password policy](../explanation/auth.md#what-a-password-has-to-be) before the
database is touched: too short or too well-known and it exits 2 with the reason
on stderr, having created nothing.

## `volunteerdb.jobs` — scheduled one-shot jobs

```sh
python -m volunteerdb.jobs.fetch_pages
python -m volunteerdb.jobs.drive_sync apply /sync
python -m volunteerdb.jobs.drive_sync record /sync
python -m volunteerdb.jobs.proposal_digest [--today YYYY-MM-DD]
python -m volunteerdb.jobs.event_reminders [--today YYYY-MM-DD]
```

`fetch_pages` refreshes every team home page from its public Google Doc
(see [Publish a team home page](../how-to/team-home-pages.md)); each team
fetches in its own transaction, so one bad doc cannot block the rest.
`drive_sync` is the Python half of the nightly roster sync (see
[Sync team rosters with Google Sheets](../how-to/drive-roster-sync.md)):
it only reads and writes a work directory — rclone on the host does all the
Drive traffic. `proposal_digest` emails each proposal voter one nightly
digest of what needs their input; `event_reminders` emails each volunteer
their event notices (scheduled by a manager / serving within
`VDB_EVENT_REMINDER_DAYS`). Both digests are per-person idempotent via
notification stamps — a failed send retries the next night — and take
`--today` for manual runs and tests. All need `VDB_DATABASE_URL`. In
production the host crontab runs them in one-shot app containers at 02:30
(`volunteerdb-drive-sync`), 03:00 (`volunteerdb-fetch-pages`), 03:30
(`volunteerdb-proposal-digest`) and 04:00 (`volunteerdb-event-reminders`);
journal tags match the script names. Nothing inside the app schedules jobs
— periodic work belongs to the host crontab (see the backup pattern in
`deploy/deploy.py`).

## `healthcheck.py` — container health probe

```sh
python /app/healthcheck.py
```

Exits 0 iff `http://127.0.0.1:$VDB_PORT/login` responds. Referenced by the
app quadlet's `HealthCmd`; not normally run by hand.

## `pyinfra` — production deploy

```sh
uvx pyinfra sttimothyto-prod deploy/deploy.py -y
```

Deploys/updates the whole containerized stack on the server. Optional
environment: `VDB_ADMIN_PASSWORD` (runs the admin bootstrap),
`VDB_SMTP2GO_API_KEY` (sets/rotates the mail key). Add `--dry` to preview.
See [Deploy and upgrade production](../how-to/deploy.md).

## `pytest` — test suite

```sh
uv run pytest
```

Needs the development database container running. See
[Run the test suite](../how-to/run-tests.md).

## Documentation build

```sh
uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
```

Sphinx comes from the local checkout at `/home/ben/sphinx` (wired via
`[tool.uv.sources]` in `pyproject.toml`).
