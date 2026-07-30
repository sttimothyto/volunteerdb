# Run the test suite

The suite (29 modules under `tests/`) runs against a real PostgreSQL — the
same dev container the app uses — in a separate `volunteerdb_test` database.

**Prerequisite:** the database container is up:

```sh
podman compose up -d db
```

If that command fails with `RuntimeError: set in .env`, set a non-empty
`VDB_STORAGE_SECRET` in your `.env`. podman-compose interpolates *every*
service before starting the one you asked for, including the profiled `app`
service whose `${VDB_STORAGE_SECRET:?set in .env}` rejects an empty value — so
an unset secret blocks even `up -d db`. Setting a real secret is worth doing
anyway: an empty one means a fresh random key per boot, logging everyone out
on every restart. See [Install and run](../tutorials/install-and-run.md).

## Run

```sh
uv run pytest                       # whole suite
uv run pytest tests/test_api.py     # one module
uv run pytest -k roster             # by keyword
uv run pytest --cov                 # with coverage (skip_covered configured)
```

## What the fixtures do

`tests/conftest.py` drops and recreates `volunteerdb_test` once per session,
migrates it with a real `alembic upgrade head`, and truncates all tables
between tests (identities restart). API tests drive the FastAPI app
in-process over `httpx.ASGITransport` — no server or browser is started.
UI tests use NiceGUI's user simulation via `tests/ui_sim_main.py`; the
`real_app_client` fixture reuses that harness to reach the fully assembled
`create_app()` over HTTP.

The suite cannot run under `pytest-xdist`: `_quiet_structlog` and `log_records`
call the process-global `structlog.configure()`, so parallel workers would
fight over logging configuration.

## Failure modes

Suite fails immediately with "Postgres unavailable"
: The database is unreachable. Start the db container (see above) and check
  `podman ps`. This is a **failure**, not a skip, on purpose: a skip exits 0,
  so a suite that never reached the database would report the same green as a
  suite that passed. To run the handful of DB-free tests anyway, set
  `VDB_TEST_ALLOW_NO_DB=1`.

Migration error at session start
: The scratch database is rebuilt every run, so a failure here means the
  migration chain itself is broken — fix the newest revision (see
  [Write a database migration](write-a-migration.md)).

Login/throttle tests interfering after repeated runs
: They shouldn't — an autouse fixture clears the in-process throttle state
  between tests. If you see 429s in a test you wrote, add your test's
  failures to that expectation rather than sleeping.

A UI-simulation assertion fails only in the full run
: `user.should_see()` retries three times at 0.1 s — a 300 ms budget that an
  argon2 verify plus a redirect can exceed when the whole suite is competing
  for the database. Pass `retries=` rather than adding a sleep.

Tests pass locally but the app misbehaves
: The suite covers services and API thoroughly and the UI via simulation;
  verify visual behavior against a seeded dev instance
  ([tutorial](../tutorials/install-and-run.md)).
