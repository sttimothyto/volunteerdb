"""Test setup: real Postgres required (podman compose up -d db).

A separate volunteerdb_test database is dropped/recreated per test session and
migrated with alembic; every test starts from truncated tables.
"""

import asyncio
import os
import re
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import structlog
from fastapi import FastAPI
from nicegui.testing.user_simulation import user_simulation
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from volunteerdb import audit, db, log
from volunteerdb import env as env_mod
from volunteerdb.api import api_router
from volunteerdb.api.deps import install_exception_handlers
from volunteerdb.config import settings
from volunteerdb.log import shared_processors
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.fakes import (  # noqa: F401 -- re-exported for the tests that import them from here
    FakeClock,
    FakeRng,
    RecordingMailer,
)
from tests.fp_helpers import ok

# Go through Settings rather than os.environ so the suite reads .env exactly
# like the app does. Reading the environment alone meant an edited
# VDB_DATABASE_URL (or POSTGRES_PASSWORD) in .env gave you a working `make
# dev` and a `make test` that failed against stale credentials, since make
# exports nothing from that file.
BASE_URL = settings().database_url
TEST_URL = BASE_URL.rsplit("/", 1)[0] + "/volunteerdb_test"

# NiceGUI "main file" for user_simulation; see its docstring for why page module
# imports have to happen inside the simulation's reset context.
SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

# The test engine, for the suite's own session helper below and for the
# simulated app (tests/ui_sim_main.py builds its Env over it). Set by the
# session-scoped `database` fixture; there is no process-global engine.
ENGINE: AsyncEngine | None = None
SESSIONS: async_sessionmaker[AsyncSession] | None = None


@asynccontextmanager
async def db_session(user_id: int | None = None) -> AsyncIterator[AsyncSession]:
    """One unit of work on the test engine, for fixtures and test setup --
    the same contract as db.transaction(), without an Env to hand in."""
    assert SESSIONS is not None, "the database fixture has not run"
    async with db._unit_of_work(SESSIONS, user_id) as session:
        yield session


# should_see budgets 3 retries x 0.1 s = 300 ms, which is the same order as one
# argon2 pass: measured at 106 ms to hash and 63 ms to verify on an idle dev box,
# at time_cost=3 / 64 MB / parallelism=4 (volunteerdb.auth), and several times
# that when the rest of the suite is competing for the same cores. Any assertion
# waiting on a sign-in, an emailed code, an invite redemption or a password
# change is therefore racing argon2 on the default budget, and loses often
# enough to matter — test_volunteer_panel failed 2 runs in 10 that way, on a
# different line each time, which is what made it look like a mystery instead of
# a stopwatch. 3 s is nowhere near the real cost; the extra is only ever spent
# on a genuine failure.
SLOW = 30


async def _now() -> datetime:
    """A timestamp that sits strictly between the write before it and the write
    after it, for as-of assertions.

    The sleeps are a clock_timestamp() granularity margin: without them a
    snapshot taken right after a write can land on the same tick as the write,
    and the union read picks up a row the test means to be looking past. Shared
    by test_history.py and test_team_cache.py, which must not drift apart on it.
    """
    await asyncio.sleep(0.02)
    now = datetime.now(UTC)
    await asyncio.sleep(0.02)
    return now


# The compiled SQL of a live team-tree read (services.teams.tree). Unique to it:
# `session.get(Team, ...)` compiles to a WHERE on the primary key with no ORDER
# BY, and the sibling tables spell themselves `team_sheet` / `team_history`, so
# neither can be mistaken for this. Snapshot reads order by the union alias
# instead, hence the alternation.
TEAM_TREE_SQL = r"FROM team ORDER BY team\.name|team_asof ORDER BY team_asof\.name"


@contextmanager
def count_sql(pattern: str):
    """Statements executed on the app engine matching `pattern`, as a list that
    fills while the block runs.

    Modelled on scripts/bench.py's capture_sql. Nothing else in the suite asserts
    a query count; this exists so the team-tree memo cannot silently regress into
    the per-caller reads it replaced (tests/test_team_cache.py).
    """
    seen: list[str] = []
    matcher = re.compile(pattern)
    assert ENGINE is not None, "the database fixture has not run"
    engine = ENGINE.sync_engine

    def before(conn, cursor, statement, parameters, context, executemany):
        if matcher.search(statement):
            seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", before)


async def mail_to(sent: list[tuple[str, str, str]], address: str, timeout_s=3.0):
    """The most recent (to, subject, body) sent to `address`, waiting for it.

    A page reveals its next step after awaiting the send, so a visible hint
    ought to mean the mail is already in — but the two are separated by an
    argon2 pass, and asserting on the captured list with no wait is the same
    race as a bare should_see.
    """
    for _ in range(int(timeout_s * 10)):
        if sent and sent[-1][0] == address:
            return sent[-1]
        await asyncio.sleep(0.1)
    raise AssertionError(f"no email to {address} within {timeout_s}s; sent={sent}")


@pytest.fixture(scope="session")
async def database():
    admin_engine = create_async_engine(
        BASE_URL, isolation_level="AUTOCOMMIT", connect_args={"timeout": 5}
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                sa.text("DROP DATABASE IF EXISTS volunteerdb_test WITH (FORCE)")
            )
            await conn.execute(sa.text("CREATE DATABASE volunteerdb_test"))
    except Exception as exc:  # DB not running
        # Failing (not skipping) is deliberate: a skip exits 0, so a suite that
        # never reached the database would report the same green as a suite that
        # passed. Set VDB_TEST_ALLOW_NO_DB=1 to opt into the old behaviour.
        message = (
            f"Postgres unavailable at {BASE_URL}: {exc}\n"
            "Start it with:  podman compose up -d db   (needs a .env — see README)\n"
            "To skip the database-backed tests deliberately, set VDB_TEST_ALLOW_NO_DB=1."
        )
        if os.environ.get("VDB_TEST_ALLOW_NO_DB") == "1":
            pytest.skip(message)
        pytest.fail(message, pytrace=False)
    finally:
        await admin_engine.dispose()

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env={**os.environ, "VDB_DATABASE_URL": TEST_URL},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"

    settings.cache_clear()
    os.environ["VDB_DATABASE_URL"] = TEST_URL
    global ENGINE, SESSIONS
    engine = db.make_engine(TEST_URL)
    ENGINE, SESSIONS = engine, db.make_sessions(engine)
    yield engine
    await engine.dispose()
    ENGINE = SESSIONS = None


@pytest.fixture(scope="session")
async def all_tables(database) -> tuple[str, ...]:
    async with database.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT tablename FROM pg_tables"
                " WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        return tuple(r[0] for r in rows)


@pytest.fixture(autouse=True)
def clean_tables(request):
    """Every test starts from truncated tables -- except one marked `pure`,
    which never touches the database at all. The database fixture is pulled
    on demand rather than declared, so `uv run pytest -m pure` runs with no
    Postgres in sight (docs/how-to/run-tests.md)."""
    if request.node.get_closest_marker("pure"):
        return
    request.getfixturevalue("_truncated_tables")


@pytest.fixture
async def _truncated_tables(database, all_tables):
    async with database.begin() as conn:
        await conn.execute(
            sa.text(f"TRUNCATE {', '.join(all_tables)} RESTART IDENTITY CASCADE")
        )
    yield


def _drop_all_events(logger, method_name, event_dict):
    raise structlog.DropEvent


@pytest.fixture(scope="session", autouse=True)
def _quiet_structlog():
    """Silence the audit listeners' output; tests opt back in via log_records."""
    structlog.configure(processors=[_drop_all_events], cache_logger_on_first_use=False)
    yield


@pytest.fixture
def log_records():
    """Captured structlog event dicts (structlog does not feed pytest's caplog)."""
    capture = structlog.testing.LogCapture()
    structlog.configure(
        processors=[*shared_processors, capture], cache_logger_on_first_use=False
    )
    yield capture.entries
    structlog.configure(processors=[_drop_all_events], cache_logger_on_first_use=False)


# --- the Env ------------------------------------------------------------------
#
# The app's impure parts as one value (volunteerdb/env.py). A test that goes
# through the real app (user_simulation) gets the real Env create_app() builds;
# the bare router app below is handed this one, whose mailer records instead
# of sending. The clock is the real one unless a test installs a FakeClock,
# which is how "now" becomes an input rather than a race.


@pytest.fixture
async def env(database) -> env_mod.Env:
    return env_mod.build(engine=database, mailer=RecordingMailer())


@pytest.fixture
def api_app(database, env) -> FastAPI:
    """The API routers on a bare app. Exposed separately so tests can introspect
    it (openapi()) without reaching into the client's private transport."""
    app = FastAPI()
    app.state.env = env
    install_exception_handlers(app)
    app.include_router(api_router)
    return app


@pytest.fixture
async def client(api_app):
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def real_app_client(database):
    """An HTTP client bound to the *real* create_app(), not the bare router app:
    middleware, mounts and page routes included. user_simulation enters the
    lifespan context, so the deferred /api routers are resolved."""
    async with user_simulation(main_file=SIM_MAIN) as user:
        yield user.http_client


@pytest.fixture
def debug_logging(monkeypatch):
    """settings() is lru_cached, so patch the module attribute the readers call."""
    original = settings()
    patched = original.model_copy(update={"log_level": "DEBUG"})
    for module in (audit, log):
        monkeypatch.setattr(module, "settings", lambda: patched)
    yield patched


@pytest.fixture
async def seeded(database):
    async with db_session() as session:
        team = ok(await teams.create(session, None, "Liturgy"))
        v = ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "maria@example.org"
            )
        )
        ok(await memberships.assign(session, None, v.id, team.id, TeamRole.member))
        ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                password="secret-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        ok(
            await users.create(
                session,
                "member@example.org",
                volunteer_id=v.id,
                password="member-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        return {"team_id": team.id, "volunteer_id": v.id}


async def _token(client, email, password) -> dict:
    """Log in over the API and return an Authorization header dict."""
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
async def token_admin(client, seeded) -> dict:
    return await _token(client, "admin@example.org", "secret-pass-phrase")


@pytest.fixture
async def token_member(client, seeded) -> dict:
    return await _token(client, "member@example.org", "member-pass-phrase")


@pytest.fixture
async def token_leader(client, seeded) -> dict:
    """A leader of the seeded Liturgy team (not an admin)."""
    async with db_session() as session:
        lena = ok(
            await volunteers.create(session, None, "Lena", "Leader", "lena@example.org")
        )
        ok(
            await memberships.assign(
                session, None, lena.id, seeded["team_id"], TeamRole.leader
            )
        )
        ok(
            await users.create(
                session,
                "lena@example.org",
                volunteer_id=lena.id,
                password="leader-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
    return await _token(client, "lena@example.org", "leader-pass-phrase")


@pytest.fixture
async def token_core(client, seeded) -> dict:
    """A core member of the seeded Liturgy team: full-roster rights, no
    management rights — the boundary most permission tests care about."""
    async with db_session() as session:
        cora = ok(
            await volunteers.create(session, None, "Cora", "Core", "cora@example.org")
        )
        ok(
            await memberships.assign(
                session, None, cora.id, seeded["team_id"], TeamRole.core
            )
        )
        ok(
            await users.create(
                session,
                "cora@example.org",
                volunteer_id=cora.id,
                password="core-pass-phrase-1",
                invite=mint.fresh_invite(),
            )
        )
    return await _token(client, "cora@example.org", "core-pass-phrase-1")
