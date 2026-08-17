"""Secrets that manage themselves.

Not a @deploy step — it declares no operations. It reads the env file already
on the server and decides, per value, whether to reuse, take from the
environment, or generate. There is no vault to stand up, nothing secret in the
repository, and re-running the deploy is always safe.

Precedence differs by kind, deliberately:

  * storage secret and database password — generated once, then reused
    forever. Never taken from the environment: rotating them has consequences
    (every session invalidated; a role that must be ALTERed to match) and
    belongs in docs/how-to/rotate-secrets.md, not in an env var somebody might
    set by accident.
  * mail key, template sheet URL, public base URL — environment wins, then
    the value on the host, then a default. Passing one is how you set or
    rotate it; omitting it keeps what is there.
"""

import os
import secrets as pysecrets
from dataclasses import dataclass

from pyinfra import host
from pyinfra.facts.files import FileContents


@dataclass(frozen=True)
class Secrets:
    storage_secret: str
    db_password: str
    smtp2go_api_key: str
    template_sheet_url: str
    public_base_url: str
    database_url: str


def _remote_env(env_file: str) -> dict[str, str]:
    lines = host.get_fact(FileContents, path=env_file) or []
    pairs = (
        line.split("=", 1)
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {k.strip(): v.strip() for k, v in pairs}


def resolve(
    site, *, env_file: str, db_user: str, db_name: str, db_host: str
) -> Secrets:
    existing = _remote_env(env_file)

    smtp2go_api_key = (
        os.environ.get("VDB_SMTP2GO_API_KEY")
        or existing.get("VDB_SMTP2GO_API_KEY")
        or ""
    )
    if not smtp2go_api_key:
        print("NOTE: VDB_SMTP2GO_API_KEY not set - emails will be logged, not sent.")

    db_password = existing.get("VDB_DB_PASSWORD") or pysecrets.token_hex(24)
    return Secrets(
        storage_secret=existing.get("VDB_STORAGE_SECRET") or pysecrets.token_hex(32),
        db_password=db_password,
        smtp2go_api_key=smtp2go_api_key,
        # Set once, after the template sheet exists on Drive; reused after.
        template_sheet_url=(
            os.environ.get("VDB_TEMPLATE_SHEET_URL")
            or existing.get("VDB_TEMPLATE_SHEET_URL")
            or ""
        ),
        # Absolute origin for links in nightly-job emails, which have no live
        # request to derive one from. Defaults to the site's own domain.
        public_base_url=(
            os.environ.get("VDB_PUBLIC_BASE_URL")
            or existing.get("VDB_PUBLIC_BASE_URL")
            or site.public_base_url
        ),
        database_url=(
            f"postgresql+asyncpg://{db_user}:{db_password}@{db_host}:5432/{db_name}"
        ),
    )
