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
- **Cytoscape.js graph** of volunteers ↔ teams, front and center on the dashboard
- Collaborative **planning**: leadership vacancies with volunteer proposals
  (propose → accept/decline), and leadership-only **workload** scoring

## Documentation

Full documentation lives in [`docs/`](docs/index.md), organized as
tutorials, how-to guides, explanation, and reference
([Diátaxis](https://documentation.divio.com/)). A running instance serves
it at **`/manual`** (signed-in users; book icon in the header) — the
container image builds it in. Build locally with:

```sh
uv run --group docs sphinx-build -W -b html docs docs/_build/html
```

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and podman (`sudo apt install podman podman-compose`).

```sh
make seed   # .env, db container, schema, demo data (prints logins)
make dev    # serve http://localhost:8080 with auto-reload
```

`make` on its own lists every target. The long form is no secret — each
target is a one-line wrapper:

```sh
cp .env.example .env            # VDB_STORAGE_SECRET may stay empty in dev
podman compose up -d db         # PostgreSQL 17 with a persistent volume
uv run alembic upgrade head     # create the schema
uv run python scripts/seed.py   # demo data + admin account (prints logins)
uv run volunteerdb              # serve http://localhost:8080
```

`make seed` writes `.env` with a generated `VDB_STORAGE_SECRET` if the file is
missing, and never touches one you have already edited. An empty secret works
too — the app falls back to a random per-boot key, which just means sessions
reset on restart.

If you start the db by hand, the `.env` step is **not** optional: podman-compose
does not apply a `${VAR:-default}` fallback when the variable name matches the
key it is assigned to, so with no `POSTGRES_PASSWORD` set the db container bakes
the literal `${POSTGRES_PASSWORD:-volunteerdb}` in as its password. If that
happened, recreate it with `make clean` (`podman compose down -v`). Going
through `make` avoids this entirely — it writes `.env` first.

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

- GUI: the dashboard (with its graph) and every team/volunteer page has a
  "View as of" date picker (read-only snapshot, amber banner)
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

A row carrying an email is matched by email alone (the name breaks
family-shared-email ties); only a row with a blank email falls to exact-name
matching. An email that matches nobody therefore creates a *new* volunteer even
when the name is already on file — the report warns, but set the address on the
existing record first if they are the same person. Imports only add/update —
they never delete, and a blank cell never clears a field — and are
**all-or-nothing**: any error rejects the whole file with a row-by-row report.
The GUI always dry-runs first and shows the report before you apply.

## Operations

- **Backup**: production backs itself up nightly (`pg_dump` → gzip →
  encrypted upload to Google Drive via an rclone crypt remote; see the
  manual's backup how-to). Ad-hoc:
  `podman exec <db-container> pg_dump -U volunteerdb volunteerdb > backup.sql`
- **Migrations**: `make migrate` (`uv run alembic upgrade head`) after pulling
- **Tests**: `make test` — starts the db container if needed, then runs
  `uv run pytest` against a separate `volunteerdb_test` database. Pass extra
  arguments with `make test ARGS="-k roster"`.
- **Start over**: `make fresh` wipes the database volume, re-migrates, re-seeds
- Dev auto-reload: `make dev` (`VDB_RELOAD=true uv run python -m
  volunteerdb.main`). It must be the `python -m` form — under the
  `volunteerdb` console script, reload respawns a worker that never reaches
  the `__mp_main__` guard, and startup fails with "You must call ui.run()".
