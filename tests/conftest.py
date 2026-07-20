"""Test setup: real Postgres required (podman compose up -d db).

A separate volunteerdb_test database is dropped/recreated per test session and
migrated with alembic; every test starts from truncated tables.
"""

import os
import subprocess

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from volunteerdb import db, throttle
from volunteerdb.api import api_router
from volunteerdb.api.deps import install_exception_handlers
from volunteerdb.config import settings
from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

BASE_URL = os.environ.get(
    "VDB_DATABASE_URL", "postgresql+asyncpg://volunteerdb:volunteerdb@localhost:5432/volunteerdb"
)
TEST_URL = BASE_URL.rsplit("/", 1)[0] + "/volunteerdb_test"


@pytest.fixture(scope="session")
async def database():
    admin_engine = create_async_engine(
        BASE_URL, isolation_level="AUTOCOMMIT", connect_args={"timeout": 5}
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(sa.text("DROP DATABASE IF EXISTS volunteerdb_test WITH (FORCE)"))
            await conn.execute(sa.text("CREATE DATABASE volunteerdb_test"))
    except Exception as exc:  # DB not running
        pytest.skip(f"Postgres unavailable at {BASE_URL}: {exc}")
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


@pytest.fixture
async def client(database):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(api_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded(database):
    async with db_session() as session:
        team = await teams.create(session, "Liturgy")
        v = await volunteers.create(session, "Maria", "Alvarez", "maria@example.org")
        await memberships.assign(session, v.id, team.id, TeamRole.member)
        await users.create(session, "admin@example.org", is_admin=True, password="secret-pw")
        await users.create(session, "member@example.org", volunteer_id=v.id, password="member-pw")
        return {"team_id": team.id, "volunteer_id": v.id}


async def _token(client, email, password) -> dict:
    """Log in over the API and return an Authorization header dict."""
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
async def token_admin(client, seeded) -> dict:
    return await _token(client, "admin@example.org", "secret-pw")


@pytest.fixture
async def token_member(client, seeded) -> dict:
    return await _token(client, "member@example.org", "member-pw")
