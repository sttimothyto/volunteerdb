# Run the test suite

- The suite runs against a real PostgreSQL: the same dev container the app
  uses, in a separate `volunteerdb_test` database.
- Nearly all of it is headless. The handful of tests under `tests/e2e/`
  drive a real browser.

**Prerequisites:**

1. Start the database container. `make test` does that for you: it starts
   the container, waits until Postgres accepts queries, and then runs the
   suite. To start the container by hand:

   ```sh
   podman compose up -d db
   ```

2. Download Playwright's Chromium, once per machine:

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
uv run pytest --cov                 # with coverage (line + branch)
uv run pytest -p randomly --randomly-seed=1234   # repeat one shuffled order
```

Every run shuffles the order tests execute in (pytest-randomly) and prints
the seed it used; pass that seed back to repeat an order that failed. Every
test has a wall limit of three minutes (pytest-timeout), so a hang fails its
own test rather than the whole run.

Tests marked `pure` (`pytestmark = pytest.mark.pure`) never touch the
database, and they run with the container down. They cover the toolkit, the
parsers, the policy rules, the jobs' plans, and the structural sweeps:

```sh
uv run pytest -m pure                    # no Postgres, no browser, seconds
```

The sweeps are the ratchets the architecture rests on
([Architecture](../explanation/architecture.md#the-layering-rule)). Each
fails on the first regression and names the line at fault:

| Sweep | Fails on |
|---|---|
| `test_purity_layer.py` | a `raise`, a clock read, `settings()`, a random draw or session-making under `services/`, `sheets/` and the pure leaves |
| `test_authorization_layer.py` | a service that takes an `Actor` and never checks it; a gate whose `Err` is dropped; a `require()`/`gate()` at a front door that is not on the allowlist |
| `test_effects_layer.py` | a `send_email(`, `audit_log(` or throttle charge under `api/`, `ui/` or `services/` — effects go through the interpreter |
| `test_ui_layer.py` | a `ui.*` call inside a session block, a `nonlocal`, a nested handler that writes into its parent's container |

The browser tests take the same arguments plus Playwright's own:

```sh
uv run pytest tests/e2e                  # just the browser tests
uv run pytest tests/e2e --headed         # watch them drive the browser
uv run pytest tests/e2e --slowmo 400     # ... slowly enough to follow
uv run pytest tests/e2e --tracing retain-on-failure   # trace a failure
uv run playwright show-trace test-results/*/trace.zip
```

## What the fixtures do

`tests/conftest.py`:

- It drops and recreates `volunteerdb_test` once per session.
- It migrates that database with a real `alembic upgrade head`.
- It truncates all tables between tests (identities restart).
- There is no process-global engine. The `database` fixture keeps the test
  engine.
- `conftest.db_session()` is the suite's own unit of work over that engine,
  for fixtures and setup.
- The `clock` fixture is a `FakeClock` frozen at the moment the test starts.
  Move it with `clock.advance(days=7)` and pass `clock.now()` on. That is
  how an invite lapses or a code expires in a test. Never edit a row for it.
- The `env` fixture builds an `Env` on the engine with a `RecordingMailer`
  and the test's `clock`. Read `env_sent` for what a service or job mailed.
- `env.with_(...)`, `FakeRng`, `FakeHttp`, `FailingMailer`,
  `RaisingSessions` and `FakeGoogleCalendar` live in `tests/fakes.py`.
- `before_latest_write(session, Model, id)` is the instant just before a
  row's current version began, read from its `sys_period`: the exact
  as-of timestamp, with no sleeps.
- API tests drive the FastAPI app in-process over `httpx.ASGITransport`. No
  server or browser starts.
- UI tests use NiceGUI's user simulation through `tests/ui_sim_main.py`.
  That module builds the simulated app's `Env` over the same engine, with
  `tests.fakes.SIM_MAILER` as its mailer and `SIM_CLOCK` as its clock. The
  `sim_sent` fixture is what the simulated app mailed during this test. It
  is emptied before every test. `SIM_CLOCK.advance(...)` moves the app's
  time, and the clock is reset between tests.
- `only(user.find(...))` is the one element a `find` matched. Prefer it to
  `.elements.pop()`, which picks arbitrarily from a set.
- `mint.today()` is the parish day, `mint.now()` the instant; build dates
  from those, never from `date.today()`, which is the machine's.
- The `real_app_client` fixture reuses that harness to reach the fully
  assembled `create_app()` over HTTP.

`tests/e2e/conftest.py` adds a browser layer on top of the same database:

- One `python -m volunteerdb.main` process runs for the session, on a free
  port, with the scheduler off and its NiceGUI storage in a temp directory.
- Playwright's Chromium drives it.
- Nothing there is a stub or a shortcut: to sign in, a test fills the login
  form.
- A test belongs in that package only when a browser is what makes it
  possible. That means a real drag, the CSS cascade, or the JavaScript in
  `ui/cytoscape_graph.js` and `ui/static/column_drag.js`.
- Uncaught JavaScript errors fail the test they happen in.

The suite cannot run under `pytest-xdist`. `_quiet_structlog` and
`log_records` call the process-global `structlog.configure()`, so parallel
workers would fight over the log configuration.

## Failure modes

Suite fails immediately with "Postgres unavailable"
: The database is unreachable. Use `make test`, which starts the container
  and waits for it, or start the container yourself and check `podman ps`.
  This is a **failure**, not a skip, on purpose. A skip exits 0, so a suite
  that never reached the database would report the same green as a suite
  that passed. To run the handful of DB-free tests anyway, set
  `VDB_TEST_ALLOW_NO_DB=1`.

Suite fails immediately with "Playwright's Chromium is not installed"
: Run `uv run playwright install --with-deps chromium`. This is a
  **failure** rather than a skip, for the same reason as the database above.
  To run everything else on purpose, set `VDB_TEST_ALLOW_NO_BROWSER=1`.

Migration error at session start
: The suite rebuilds the scratch database every run, so a failure here means
  the migration chain itself is broken. Fix the newest revision (see
  [Write a database migration](write-a-migration.md)).

Login/throttle tests interfere after repeated runs
: They do not. An autouse fixture clears the in-process throttle state
  between tests. If you see 429s in a test you wrote, add your test's
  failures to that expectation; do not add a sleep.

A browser test fails only in the full run
: Rerun it alone (`uv run pytest tests/e2e/test_browser_graph.py`) to
  confirm. Then re-run with `--tracing retain-on-failure` and open the trace.
  The trace has the DOM, the console and a screenshot at every step.
  `expect()` waits 15 s here, not its 5 s default, so a real timeout is a
  real problem, not a slow machine.

A UI-simulation assertion fails only in the full run
: `user.should_see()` retries 3 times at 0.1 s. That is a 300 ms budget,
  and an argon2 verify plus a redirect can exceed it when the whole suite
  competes for the database. Pass `retries=`; do not add a sleep.

Tests pass locally but the app misbehaves
: The suite covers services and API thoroughly, the UI through simulation,
  and a few browser-only seams under `tests/e2e/`. It does not look at the
  app. Verify visual behavior against a seeded dev instance
  ([tutorial](../tutorials/install-and-run.md)).
