"""Site configuration for the deploy: what differs per parish, and what does not.

Two layers live here, and the split is the point:

  * ``Site`` — loaded from ``deploy/sites/<name>.toml``. Everything a second
    parish has to change, and nothing else. Eleven or so keys, in a file an
    operator can read without knowing Python.
  * The module constants below — paths, image and network names, the
    container UID. Identical on every instance; a developer changes them when
    the stack changes, not when a parish does.

Stdlib only, deliberately: ``tests/test_deploy_config.py`` imports this to
check the site files, and the test suite has no pyinfra.

Named ``siteconf`` rather than ``site`` because ``site`` is a stdlib module.
pyinfra execs the deploy file without putting its directory on ``sys.path``,
so importers must ``sys.path.insert(0, str(HERE))`` first — see deploy.py.
"""

import tomllib
from dataclasses import dataclass, fields
from datetime import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITES_DIR = HERE / "sites"

# --- the same on every site -------------------------------------------------
# Change these when the stack changes, never to stand up another parish.

APP_DIR = "/opt/volunteerdb/app"  # synced source == podman build context
ENV_FILE = "/etc/volunteerdb/env"
DB_ENV_FILE = "/etc/volunteerdb/db.env"
QUADLET_DIR = "/etc/containers/systemd"
SYSTEMD_DIR = "/etc/systemd/system"

IMAGE = "localhost/volunteerdb:latest"
PG_IMAGE = "docker.io/library/postgres:17"
NET = "volunteerdb"
APP_PORT = 8080  # port inside the app container; mirrors config.py's default
DB_NAME = DB_USER = "volunteerdb"
DB_CONTAINER = "volunteerdb-db"
APP_UID = 10001  # the image's `app` user; owns the bind-mounted sync workdir

# Conservative tuning for a >=4 GB host shared with the app container. The
# development compose file deliberately does NOT mirror this — a laptop has no
# use for 512 MB of shared_buffers against a seeded demo database.
PG_TUNING = (
    "postgres -c shared_buffers=512MB -c effective_cache_size=2GB "
    "-c work_mem=16MB -c maintenance_work_mem=128MB -c random_page_cost=1.1"
)

BACKUP_SCRIPT = "/usr/local/bin/volunteerdb-backup"
BACKUP_DIR = "/var/backups/volunteerdb"
RCLONE_CONF = "/root/.config/rclone/rclone.conf"
SYNC_WORKDIR = "/var/lib/volunteerdb-drive-sync"
DRIVE_SYNC_SCRIPT = "/usr/local/bin/volunteerdb-drive-sync"
DECORATE_SCRIPT = "/usr/local/bin/volunteerdb-decorate-sheets"

# What files.sync ships to APP_DIR, which is also the podman build context.
# Kept separate from .containerignore on purpose: the glob semantics differ
# (exclude is fnmatch over FULL REMOTE paths, hence the */ prefixes, and
# exclude_dir affects traversal but not deletion), and the two lists are not
# mirror images — Containerfile and .containerignore are excluded from the
# image but must still be synced, since they are what the build reads.
SYNC_EXCLUDE = (
    "*.pyc",
    "*/.env",
    "*/.env.example",
    "*/.gitignore",
    "*/compose.yaml",
)
SYNC_EXCLUDE_DIR = (
    ".git",
    ".github",  # CI workflows: not part of the app or the build context
    ".venv",
    ".nicegui",
    ".claude",
    ".pytest_cache",
    ".ruff_cache",
    "tests",
    "scripts",
    "deploy",
    "docs/_build",  # the image's docs stage builds fresh HTML itself
    "__pycache__",
    "*/__pycache__",
)

QUADLETS = (
    "volunteerdb.network",
    "volunteerdb-db.container",
    "volunteerdb-app.container",
)
# Host-side scheduling is plain systemd timer/service units — nothing
# container-scoped about them — installed into SYSTEMD_DIR.
TIMER_UNITS = (
    "volunteerdb-backup.service",
    "volunteerdb-backup.timer",
    "volunteerdb-drive-sync.service",
    "volunteerdb-drive-sync.timer",
)


# --- what differs per site --------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One parish's deployment. Field names are ``<section>_<key>`` in the TOML."""

    # [site]
    site_name: str  # must equal the filename stem
    site_org_name: str  # appears in outbound mail; -> VDB_ORG_NAME
    site_timezone: str  # -> VDB_TIMEZONE and both systemd timers

    # [host]
    host_ssh_host: str  # pyinfra inventory name or address
    host_ssh_user: str
    host_public_ip: str  # documentation/DNS only; no operation reads it
    host_domain: str  # public hostname Caddy serves
    host_listen_port: int  # host loopback port Caddy proxies to

    # [mail]
    mail_from_address: str
    mail_from_name: str
    mail_admin_email: str  # the bootstrapped administrator
    mail_alert_email: str  # backup/sync wrapper AND in-app job failures
    mail_contact_email: str  # footer of the built manual

    # [backup]
    backup_rclone_remote: str  # the crypt wrapper's name is this + "-crypt"
    backup_retain_local_days: int
    backup_retain_remote_days: int

    # [drive_sync]
    drive_sync_sheets_folder: str
    drive_sync_revoke_public_links: bool

    # [schedule] — parish-local times, "HH:MM"
    schedule_backup_at: str
    schedule_drive_sync_at: str
    schedule_fetch_pages_at: str
    schedule_proposal_digest_at: str
    schedule_event_reminders_at: str

    @property
    def rclone_crypt_remote(self) -> str:
        """Encrypts the Drive leg: dump contents and names are ciphertext
        before upload. The plain remote stays in use for the roster sheets,
        which leaders edit in the Drive UI."""
        return f"{self.backup_rclone_remote}-crypt"

    @property
    def rclone_dest(self) -> str:
        return f"{self.rclone_crypt_remote}:"

    @property
    def public_base_url(self) -> str:
        return f"https://{self.host_domain}"


def _flatten(raw: dict) -> dict:
    return {
        f"{section}_{key}": value
        for section, entries in raw.items()
        if isinstance(entries, dict)
        for key, value in entries.items()
    }


def available() -> list[str]:
    return sorted(p.stem for p in SITES_DIR.glob("*.toml"))


def load(name: str | None = None) -> Site:
    """Load ``deploy/sites/<name>.toml``, defaulting to $VDB_SITE.

    There is deliberately no default site: on a repository that deploys more
    than one parish, guessing which is a way to deploy the wrong one.
    """
    import os

    name = name or os.environ.get("VDB_SITE") or ""
    if not name:
        raise SystemExit(
            "VDB_SITE is not set. Choose one of: "
            + (", ".join(available()) or "(no files in deploy/sites/)")
        )

    path = SITES_DIR / f"{name}.toml"
    if not path.is_file():
        raise SystemExit(
            f"No such site {name!r} ({path} does not exist). Available: "
            + (", ".join(available()) or "(none)")
        )

    flat = _flatten(tomllib.loads(path.read_text()))
    expected = {f.name for f in fields(Site)}

    # Both directions, because either one silently renders an empty string
    # into a unit file. They are reported together rather than one at a time:
    # a typo'd key produces both at once, and "missing host_listen_port" on
    # its own does not tell you that you wrote `lisen_port`.
    missing = sorted(expected - flat.keys())
    unknown = sorted(flat.keys() - expected)
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown {', '.join(unknown)}")
        raise SystemExit(f"{path}: " + "; ".join(parts))

    site = Site(**flat)
    if site.site_name != name:
        raise SystemExit(
            f"{path}: [site] name is {site.site_name!r} but the file is {name}.toml"
        )
    for field_name in (
        "schedule_backup_at",
        "schedule_drive_sync_at",
        "schedule_fetch_pages_at",
        "schedule_proposal_digest_at",
        "schedule_event_reminders_at",
    ):
        value = getattr(site, field_name)
        try:
            time.fromisoformat(value)
        except ValueError:
            raise SystemExit(f"{path}: {field_name} is not HH:MM: {value!r}") from None
    return site
