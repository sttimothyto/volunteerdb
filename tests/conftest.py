"""Test setup: real Postgres required (podman compose up -d db).

A separate volunteerdb_test database is dropped/recreated per test session and
migrated with alembic; every test starts from truncated tables.
"""

import os
import subprocess
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import structlog
from fastapi import FastAPI
from nicegui.testing.user_simulation import user_simulation
from sqlalchemy.ext.asyncio import create_async_engine

from volunteerdb import audit, db, log, throttle
from volunteerdb.api import api_router
from volunteerdb.api.deps import install_exception_handlers
from volunteerdb.config import settings
from volunteerdb.db import db_session
from volunteerdb.log import shared_processors
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

BASE_URL = os.environ.get(
    "VDB_DATABASE_URL",
    "postgresql+asyncpg://volunteerdb:volunteerdb@localhost:5432/volunteerdb",
)
TEST_URL = BASE_URL.rsplit("/", 1)[0] + "/volunteerdb_test"

# NiceGUI "main file" for user_simulation; see its docstring for why page module
# imports have to happen inside the simulation's reset context.
SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


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
    engine = db.init(TEST_URL)
    yield engine
    await engine.dispose()


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
async def clean_tables(database, all_tables):
    async with database.begin() as conn:
        await conn.execute(
            sa.text(f"TRUNCATE {', '.join(all_tables)} RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture(autouse=True)
def reset_throttle():
    """The sliding-window state is module-global and would bleed across tests."""
    throttle._hits.clear()
    yield
    throttle._hits.clear()


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


@pytest.fixture
def api_app(database) -> FastAPI:
    """The API routers on a bare app. Exposed separately so tests can introspect
    it (openapi()) without reaching into the client's private transport."""
    app = FastAPI()
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
        team = await teams.create(session, "Liturgy")
        v = await volunteers.create(session, "Maria", "Alvarez", "maria@example.org")
        await memberships.assign(session, v.id, team.id, TeamRole.member)
        await users.create(
            session, "admin@example.org", is_admin=True, password="secret-pass-phrase"
        )
        await users.create(
            session,
            "member@example.org",
            volunteer_id=v.id,
            password="member-pass-phrase",
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
        lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
        await memberships.assign(session, lena.id, seeded["team_id"], TeamRole.leader)
        await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            password="leader-pass-phrase",
        )
    return await _token(client, "lena@example.org", "leader-pass-phrase")
