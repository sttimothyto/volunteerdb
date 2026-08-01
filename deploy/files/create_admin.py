"""Idempotent admin bootstrap. Needs VDB_DATABASE_URL (sourced from
/etc/volunteerdb/env) plus VDB_ADMIN_EMAIL / VDB_ADMIN_PASSWORD. Safe to re-run."""

import asyncio
import os
import sys

from volunteerdb.db import db_session
from volunteerdb.log import init_logging
from volunteerdb.services import users


async def main() -> int:
    init_logging()
    email = os.environ["VDB_ADMIN_EMAIL"]
    password = os.environ["VDB_ADMIN_PASSWORD"]
    async with db_session() as session:
        existing = await users.get_by_email(session, email)
        if existing is not None:
            print(
                f"admin {existing.email} already exists (id={existing.id}); nothing to do"
            )
            return 0
        user = await users.create(session, email, is_admin=True, password=password)
        print(f"created admin {user.email} (id={user.id})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
