"""Idempotent admin bootstrap: ``python -m volunteerdb.admin_bootstrap``.

Needs VDB_DATABASE_URL (sourced from /etc/volunteerdb/env in production) plus
VDB_ADMIN_EMAIL / VDB_ADMIN_PASSWORD. Safe to re-run — an existing account is
left exactly as it is, so the deploy can call this on every run.

It lives in the package rather than deploy/ so that the image built by a plain
``podman build .`` can run it: .containerignore excludes deploy/ entirely, and
a bootstrap the image cannot execute is a trap for anyone reproducing the
production image by hand.
"""

import asyncio
import os
import sys

from volunteerdb.db import db_session
from volunteerdb.log import init_logging
from volunteerdb.passwords import WeakPassword
from volunteerdb.passwords import check as check_password
from volunteerdb.services import users


async def main() -> int:
    init_logging()
    email = os.environ["VDB_ADMIN_EMAIL"]
    password = os.environ["VDB_ADMIN_PASSWORD"]
    try:
        # Checked before the database is touched so a rejected VDB_ADMIN_PASSWORD
        # fails the deploy with one readable line instead of a traceback.
        check_password(password, email=email)
    except WeakPassword as exc:
        print(f"VDB_ADMIN_PASSWORD rejected: {exc}", file=sys.stderr)
        return 2
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
