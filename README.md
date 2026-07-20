# VolunteerDB (VDB) — St. Timothy Parish

Who serves where? ~500 volunteers across ~50 ministry teams (with sub-teams),
each volunteer holding a role per team. Built to answer the priest's question
in a transient parish: **"when this parishioner leaves, what holes do I have
to fill?"**

- **Web GUI** (NiceGUI) + **JSON API** (FastAPI) in one process, many
  concurrent users on one live PostgreSQL database
- Fourfold roles per team — *Ministry leader, Second-in-command, Core team
  member, Member* — drive both the data model and access control
- Full **history tracking**: view any team, volunteer, or the whole graph
  *as of any past date*
- Spreadsheet **import/export** (.xlsx) that round-trips
- **Cytoscape.js graph** of volunteers ↔ teams

## Documentation

Full documentation lives in [`docs/`](docs/index.md), organized as
tutorials, how-to guides, explanation, and reference
([Diátaxis](https://documentation.divio.com/)). Build it with:

```sh
uv run --group docs sphinx-build -W -b html docs docs/_build/html
```

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and podman (`sudo apt install podman podman-compose`).

```sh
cp .env.example .env            # set VDB_STORAGE_SECRET to something random
podman compose up -d db         # PostgreSQL 17 with a persistent volume
uv run alembic upgrade head     # create the schema
uv run python scripts/seed.py   # demo data + admin account (prints logins)
uv run volunteerdb              # serve http://localhost:8080
```

The `.env` step is not optional under podman-compose: it does not apply
`${VAR:-default}` fallbacks for unset variables, so without `.env` the db
container initializes with a literal `${POSTGRES_PASSWORD:-volunteerdb}` as
its password. If that happened, recreate it: `podman compose down -v && podman
compose up -d db`.

Sign in with the printed admin login. Seeded demo logins:
`admin@sttimothy.example` (admin), `maria.alvarez@example.org` (a ministry
leader), `felix.garcia@example.org` (a plain member) — see seed output for
passwords.

To also containerize the app itself: `podman compose --profile app up -d --build`.

## Python tools and library

- uv instead of pip for python package and project management.

## Architecture

```
src/volunteerdb/
  models.py        SQLAlchemy models: volunteer, team (self-referencing
                   parent_team_id for sub-teams), membership (UNIQUE per
                   volunteer+team, role enum), app_user — plus *_history twins
  history.py       as-of queries: live ∪ history rows whose sys_period @> t
  permissions.py   Actor with per-team rights derived from the fourfold roles
  services/        the one business layer (used by both API and GUI)
  api/             FastAPI routers under /api (Bearer-token auth)
  ui/              NiceGUI pages (session-cookie auth) + Cytoscape element
  sheets/          xlsx template/export/import (all-or-nothing, dry-run)
migrations/        alembic; 0001 creates tables, history twins, triggers
```

The GUI runs server-side and calls the service layer directly; the JSON API is
a thin wrapper over the same services. Postgres MVCC handles concurrent use.

### History / time travel

`volunteer`, `team`, and `membership` are system-versioned by a PL/pgSQL
trigger: every UPDATE/DELETE archives the old row into `<table>_history` with
its validity period (`sys_period tstzrange`) and the acting user
(`changed_by`, from the transaction-local `app.user_id` setting).

- GUI: every team/volunteer/graph page has a "View as of" date picker
  (read-only snapshot, amber banner)
- API: add `?as_of=2026-01-01T00:00:00+02:00` to any GET

**Migration note:** any future column change on a versioned table must be
mirrored on its `_history` twin (the trigger inserts positionally).

### Access control

| Right | admin | leader/second | core | member |
|---|---|---|---|---|
| manage roster (team + sub-teams) | ✓ | ✓ | | |
| see contact details (team + sub-teams) | ✓ | ✓ | ✓ | |
| see roster names (own team) | ✓ | ✓ | ✓ | ✓ |
| browse team directory, edit own profile | ✓ | ✓ | ✓ | ✓ |
| create/edit teams, accounts, imports | ✓ | | | |

(Authoritative, finer-grained matrix: [docs/reference/permissions.md](docs/reference/permissions.md).)

Accounts are admin-provisioned via **invite links** (Accounts page → bulk or
per person), emailed automatically when `VDB_SMTP2GO_API_KEY` is set and
always available to hand out by other means. The volunteer opens the link
and sets a password — or skips it and signs in with emailed one-time codes.

## JSON API

```sh
TOKEN=$(curl -s localhost:8080/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@sttimothy.example","password":"…"}' | jq -r .token)

curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/teams
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/volunteers?q=alvarez
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/volunteers/1/impact
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/reports/coverage
curl -s -H "Authorization: Bearer $TOKEN" -o parish.xlsx localhost:8080/api/export/parish.xlsx
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parish.xlsx 'localhost:8080/api/import?dry_run=true'
```

Interactive docs at `/docs`. Endpoints: `/api/volunteers` (+`/assignments`,
`/impact`), `/api/teams` (+`/roster`), `/api/memberships`,
`/api/reports/coverage`, `/api/graph`, `/api/export/*`, `/api/import`,
`/api/users` (admin).

## Spreadsheets

One workbook, two sheets — export, edit, re-import:

- **Volunteers**: First name, Last name, Email, Phone, Notes, Active
- **Memberships**: Volunteer email, Volunteer name, Team path
  (`Liturgy / Music Ministry`), Role (label or short value), Joined on, Notes

Volunteers are matched by email (name breaks family-shared-email ties), then
by exact name. Imports only add/update — they never delete — and are
**all-or-nothing**: any error rejects the whole file with a row-by-row report.
The GUI always dry-runs first and shows the report before you apply.

## Operations

- **Backup**: `podman exec <db-container> pg_dump -U volunteerdb volunteerdb > backup.sql`
- **Migrations**: `uv run alembic upgrade head` after pulling changes
- **Tests**: `uv run pytest` (needs the db container running; uses a separate
  `volunteerdb_test` database)
- Dev auto-reload: `VDB_RELOAD=true uv run python -m volunteerdb.main`
