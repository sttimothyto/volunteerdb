# Run the test suite

The suite runs against a real PostgreSQL — the same dev container the app uses
— in a separate `volunteerdb_test` database. Nearly all of it is headless; the
handful of tests under `tests/e2e/` drive a real browser.

**Prerequisites:**

1. The database container is up. `make test` does that for you — it starts the
   container, waits until Postgres actually accepts queries, and then runs the
   suite. To start it by hand:

   ```sh
   podman compose up -d db
   ```

2. Playwright's Chromium is downloaded, once per machine:

   ```sh
   uv run playwright install --with-deps chromium
   ```

## Run

```sh
make test                           # whole suite (starts the db first)
make test ARGS="-k roster"          # extra pytest arguments

uv run pytest                       # or drive pytest directly
uv run pytest tests/test_api.py     # one module
uv run pytest -k roster             # by keyword
uv run pytest --cov                 # with coverage (skip_covered configured)
```

Tests marked `pure` (`pytestmark = pytest.mark.pure`) never touch the
database and run with the container down — the toolkit, the parsers, the
structural sweeps:

```sh
uv run pytest -m pure                    # no Postgres, no browser, seconds
```

The browser tests take the same arguments plus Playwright's own:

```sh
uv run pytest tests/e2e                  # just the browser tests
uv run pytest tests/e2e --headed         # watch them drive the browser
uv run pytest tests/e2e --slowmo 400     # ... slowly enough to follow
uv run pytest tests/e2e --tracing retain-on-failure   # trace a failure
uv run playwright show-trace test-results/*/trace.zip
```

## What the fixtures do

`tests/conftest.py` drops and recreates `volunteerdb_test` once per session,
migrates it with a real `alembic upgrade head`, and truncates all tables
between tests (identities restart). API tests drive the FastAPI app
in-process over `httpx.ASGITransport` — no server or browser is started.
UI tests use NiceGUI's user simulation via `tests/ui_sim_main.py`; the
`real_app_client` fixture reuses that harness to reach the fully assembled
`create_app()` over HTTP.

`tests/e2e/conftest.py` adds a browser layer on top of the same database: one
`python -m volunteerdb.main` process for the session, on a free port, with the
scheduler off and its NiceGUI storage in a temp directory, driven by
Playwright's Chromium. Nothing there is stubbed or shortcut — signing in means
filling the login form — and a test belongs in that package only when a
browser is what makes it possible: a real drag, the CSS cascade, or the
JavaScript in `ui/cytoscape_graph.js` and `ui/static/column_drag.js`. Uncaught
JavaScript errors fail the test they happen in.

The suite cannot run under `pytest-xdist`: `_quiet_structlog` and `log_records`
call the process-global `structlog.configure()`, so parallel workers would
fight over logging configuration.

## Failure modes

Suite fails immediately with "Postgres unavailable"
: The database is unreachable. Use `make test`, which starts the container and
  waits for it, or start it yourself and check `podman ps`. This is a
  **failure**, not a skip, on purpose: a skip exits 0,
  so a suite that never reached the database would report the same green as a
  suite that passed. To run the handful of DB-free tests anyway, set
  `VDB_TEST_ALLOW_NO_DB=1`.

Suite fails immediately with "Playwright's Chromium is not installed"
: Run `uv run playwright install --with-deps chromium`. A **failure** rather
  than a skip, for the same reason as the database above. To run everything
  else deliberately, set `VDB_TEST_ALLOW_NO_BROWSER=1`.

Migration error at session start
: The scratch database is rebuilt every run, so a failure here means the
  migration chain itself is broken — fix the newest revision (see
  [Write a database migration](write-a-migration.md)).

Login/throttle tests interfering after repeated runs
: They shouldn't — an autouse fixture clears the in-process throttle state
  between tests. If you see 429s in a test you wrote, add your test's
  failures to that expectation rather than sleeping.

A browser test fails only in the full run
: Rerun it alone (`uv run pytest tests/e2e/test_browser_graph.py`) to confirm,
  then re-run with `--tracing retain-on-failure` and open the trace: it has the
  DOM, the console and a screenshot at every step. `expect()` waits 15 s here
  rather than its 5 s default, so a genuine timeout is a genuine problem, not a
  slow machine.

A UI-simulation assertion fails only in the full run
: `user.should_see()` retries three times at 0.1 s — a 300 ms budget that an
  argon2 verify plus a redirect can exceed when the whole suite is competing
  for the database. Pass `retries=` rather than adding a sleep.

Tests pass locally but the app misbehaves
: The suite covers services and API thoroughly, the UI via simulation, and a
  few browser-only seams under `tests/e2e/`; it does not look at the app.
  Verify visual behavior against a seeded dev instance
  ([tutorial](../tutorials/install-and-run.md)).
