"""Test setup: real Postgres required (podman compose up -d db).

A separate volunteerdb_test database is dropped/recreated per test session and
migrated with alembic; every test starts from truncated tables.
"""

import os
import subprocess

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from volunteerdb import db
from volunteerdb.config import settings

BASE_URL = os.environ.get(
    "VDB_DATABASE_URL", "postgresql+asyncpg://volunteerdb:volunteerdb@localhost:5432/volunteerdb"
)
TEST_URL = BASE_URL.rsplit("/", 1)[0] + "/volunteerdb_test"

ALL_TABLES = (
    "membership_history",
    "volunteer_history",
    "team_history",
    "membership",
    "app_user",
    "volunteer",
    "team",
)


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


@pytest.fixture(autouse=True)
async def clean_tables(database):
    async with database.begin() as conn:
        await conn.execute(
            sa.text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
        )
    yield
