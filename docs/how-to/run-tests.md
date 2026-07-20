# Run the test suite

The suite (23 modules under `tests/`) runs against a real PostgreSQL — the
same dev container the app uses — in a separate `volunteerdb_test` database.

**Prerequisite:** the database container is up:

```sh
podman compose up -d db
```

## Run

```sh
uv run pytest                       # whole suite
uv run pytest tests/test_api_teams.py   # one module
uv run pytest -k roster             # by keyword
uv run pytest --cov                 # with coverage (skip_covered configured)
```

## What the fixtures do

`tests/conftest.py` drops and recreates `volunteerdb_test` once per session,
migrates it with a real `alembic upgrade head`, and truncates all tables
between tests (identities restart). API tests drive the FastAPI app
in-process over `httpx.ASGITransport` — no server or browser is started.
UI tests use NiceGUI's user simulation via `tests/ui_sim_main.py`.

## Failure modes

Suite reports *skipped* immediately
: PostgreSQL is unreachable — the suite skips rather than fails. Start the
  db container (see above) and check `podman ps`.

Migration error at session start
: The scratch database is rebuilt every run, so a failure here means the
  migration chain itself is broken — fix the newest revision (see
  [Write a database migration](write-a-migration.md)).

Login/throttle tests interfering after repeated runs
: They shouldn't — an autouse fixture clears the in-process throttle state
  between tests. If you see 429s in a test you wrote, add your test's
  failures to that expectation rather than sleeping.

Tests pass locally but the app misbehaves
: The suite covers services and API thoroughly and the UI via simulation;
  verify visual behavior against a seeded dev instance
  ([tutorial](../tutorials/install-and-run.md)).
