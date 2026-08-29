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

from volunteerdb.config import settings
from volunteerdb.db import db_session
from volunteerdb.errors import message
from volunteerdb.fp import Err
from volunteerdb.log import init_logging
from volunteerdb.passwords import check as check_password
from volunteerdb.passwords import site_terms
from volunteerdb.services import users


async def main() -> int:
    init_logging()
    email = os.environ["VDB_ADMIN_EMAIL"]
    password = os.environ["VDB_ADMIN_PASSWORD"]
    # Checked before the database is touched so a rejected VDB_ADMIN_PASSWORD
    # fails the deploy with one readable line instead of a traceback.
    s = settings()
    terms = site_terms(s.org_name, s.mail_from, s.public_base_url)
    if weak := check_password(password, email=email, site_terms=terms):
        print(f"VDB_ADMIN_PASSWORD rejected: {message(weak.error)}", file=sys.stderr)
        return 2
    async with db_session() as session:
        existing = await users.get_by_email(session, email)
        if existing is not None:
            print(
                f"admin {existing.email} already exists (id={existing.id}); nothing to do"
            )
            return 0
        made = await users.create(
            session, email, is_admin=True, password=password, site_terms=terms
        )
        if isinstance(made, Err):
            print(f"admin not created: {message(made.error)}", file=sys.stderr)
            return 2
        user, _ = made.value
        print(f"created admin {user.email} (id={user.id})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
