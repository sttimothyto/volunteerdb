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

Seeds a fresh database with a whole demo parish, shaped to exercise every
feature: 34 teams three levels deep, ~150 volunteers (some archived, some
with no email, one household sharing an address), ~290 memberships,
ended/rejoined spells and mid-spell promotions for the timeline chart, five
custom fields, placeholder headshots, ~55 events either side of today with
rosters, RSVPs, substitutions and attendance overrides, one proposal in
every state (nominating, voting, awaiting decision, appointed, cancelled,
re-opened) and six published ministry pages. A
team named **Clergy** is filled, which is all it takes to be the
{term}`clergy team`, so proposals opened on the demo data get a realistic
voting roll. Refuses to run if any volunteers exist.

Everything is deterministic — one fixed RNG seed, dates relative to today —
so reseeding reproduces the same parish.

Created logins: 33 accounts, one for the administrator and one for each
ministry leader, **all on the password `demo`**. The notable ones:

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
`demo` is four characters, so it does **not** clear the
[password policy](../explanation/auth.md#what-a-password-has-to-be).
`users.create` would refuse it; the seed writes the argon2 hash onto the
account itself instead. That bypass exists only in this script — no path a
user can reach was widened — and it is why nothing but a throwaway
development database should ever be seeded.
```

## `volunteerdb.admin_bootstrap` — admin bootstrap

```sh
VDB_ADMIN_EMAIL=... VDB_ADMIN_PASSWORD=... uv run python -m volunteerdb.admin_bootstrap
```

Idempotent: creates the admin account if missing, otherwise leaves it
untouched. It is part of the package, so any image built from this repository
can run it; the deploy runs it as a one-shot container when
`VDB_ADMIN_PASSWORD` is passed. Requires `VDB_ADMIN_EMAIL` and
`VDB_ADMIN_PASSWORD`.

This is the **only** way into a brand-new instance — pass
`VDB_ADMIN_PASSWORD` on its first deploy or there is no account to sign in
with.

`VDB_ADMIN_PASSWORD` is checked against the
[password policy](../explanation/auth.md#what-a-password-has-to-be) before the
database is touched: too short or too well-known and it exits 2 with the reason
on stderr, having created nothing.

## `volunteerdb.jobs` — scheduled one-shot jobs

```sh
python -m volunteerdb.jobs.roster_sync
python -m volunteerdb.jobs.fetch_pages
python -m volunteerdb.jobs.proposal_digest [--today YYYY-MM-DD]
python -m volunteerdb.jobs.event_reminders [--today YYYY-MM-DD]
python -m volunteerdb.jobs.calendar_sync
python -m volunteerdb.jobs.task_force_cleanup
```

`fetch_pages` refreshes every team home page from its public Google Doc
(see [Publish a team home page](../how-to/team-home-pages.md)); each team
fetches in its own transaction, so one bad doc cannot block the rest.
`roster_sync` reconciles every team's roster with its Google Sheet (see
[Sync team rosters with Google Sheets](../how-to/roster-spreadsheets.md));
each team syncs independently, so one unshared sheet cannot stop the rest,
and unconfigured it exits 0 with "not configured". It sends no mail: a pass
over a messy sheet can redirect dozens of addresses at once, and mailing
each of the old mailboxes — usually the broken ones being fixed — spent a
noticeable slice of a 200-message day for nothing. The redirects still land
in `volunteer_history`. `proposal_digest` emails each proposal voter one nightly
digest of what needs their input; `event_reminders` emails each volunteer
their event notices (scheduled by a manager, serving this week, serving
tomorrow — the reminder stages honour the per-sign-up preferences). Both
digests are per-person idempotent via notification stamps — a failed send
retries the next night — and take `--today` for manual runs and tests. All
need `VDB_DATABASE_URL`.

`calendar_sync` reconciles events onto the public parish Google Calendar
(see [Publish events to a Google Calendar](../how-to/google-calendar-sync.md));
unconfigured it exits 0 with "not configured", so it is always safe to run.
`task_force_cleanup` dismantles the auto-created task-force team of every
finished or cancelled event (see
[Events and scheduling](../explanation/events.md)).

All of these run **inside the app process**: `volunteerdb.scheduler` fires
the nightly four at their parish-local times (`VDB_ROSTER_SYNC_AT` 02:30,
`VDB_FETCH_PAGES_AT` 03:00, `VDB_PROPOSAL_DIGEST_AT` 03:30,
`VDB_EVENT_REMINDERS_AT` 04:00), `calendar_sync` every 30 minutes and
`task_force_cleanup` hourly, records completion in the `job_run` table
so a restart or redeploy cannot skip a night, retries a nightly failure
every 30 minutes (3 attempts/day) and emails `VDB_ALERT_EMAIL` on each
failed attempt — an interval job instead retries on its own cadence and
alerts at most once per day. Their output lands in the app journal (`journalctl -u
volunteerdb-app`, grep `scheduler`). The `python -m` forms above stay for
manual runs — in production:
`podman run --rm --network volunteerdb --env-file /etc/volunteerdb/env
localhost/volunteerdb:latest python -m volunteerdb.jobs.<name>`. An
advisory lock (taken by both the scheduler and the CLI) keeps a manual run
and the scheduler from working the same job concurrently.

The 02:00 backup remains the one host-side systemd timer; the roster sync
used to be the other, wrapped in rclone transfers, until reading a
link-shared sheet made the host leg unnecessary.

## `healthcheck.py` — container health probe

```sh
python /app/healthcheck.py
```

Exits 0 iff `http://127.0.0.1:$VDB_PORT/login` responds. Referenced by the
app quadlet's `HealthCmd`; not normally run by hand.

## `pyinfra` — production deploy

```sh
VDB_SITE=<site> uvx pyinfra deploy/inventory.py deploy/deploy.py -y
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

Sphinx and its extensions come from PyPI, pinned in the `docs` dependency
group in `pyproject.toml`. The container image builds the same manual in its
own stage, so `/manual` always documents the release that is running.
