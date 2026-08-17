"""
Deploy VolunteerDB as a containerized stack (podman Quadlet under systemd).

Idempotent: the same command performs a first install and a routine upgrade.

Usage — VDB_SITE names a file in deploy/sites/, which holds everything
specific to that parish; deploy/siteconf.py holds what is the same on all of
them. The inventory reads the host from the same file:

    VDB_SITE=sttimothy uvx pyinfra deploy/inventory.py deploy/deploy.py --dry
    VDB_SITE=sttimothy VDB_ADMIN_PASSWORD='...' uvx pyinfra deploy/inventory.py deploy/deploy.py -y

Optional environment: VDB_ADMIN_PASSWORD runs the admin bootstrap (required
on a brand-new instance — it is the only way in); VDB_SMTP2GO_API_KEY sets or
rotates the mail key. Both are read back from the remote env file on later
runs, so passing them again is what rotates them.

Standing up a NEW instance is docs/how-to/new-instance.md — this script is
only the part that is automated. Why it is shaped this way (quadlets, the
build on the server, self-managing secrets) is
docs/explanation/deployment.md; how to run and roll it back is
docs/how-to/deploy.md.
"""

import os
import secrets as pysecrets
import sys
from pathlib import Path

from pyinfra import host
from pyinfra.facts.files import FileContents
from pyinfra.operations import apt, files, server, systemd

# pyinfra resolves relative src paths against the invocation cwd, not this
# file — anchor everything here so the deploy works from any directory. It
# also execs this file rather than importing it, so HERE has to go on
# sys.path before siteconf can be imported.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import siteconf  # noqa: E402  (must follow the sys.path line above)

# Everything specific to this parish; everything else is a siteconf constant.
site = siteconf.load()

APP_DIR = siteconf.APP_DIR  # synced source == podman build context
ENV_FILE = siteconf.ENV_FILE
DB_ENV_FILE = siteconf.DB_ENV_FILE
QUADLET_DIR = siteconf.QUADLET_DIR
QUADLETS = siteconf.QUADLETS
IMAGE = siteconf.IMAGE
PG_IMAGE = siteconf.PG_IMAGE
NET = siteconf.NET
PORT = site.host_listen_port  # host loopback port Caddy proxies to
APP_PORT = siteconf.APP_PORT  # port inside the app container
DB_NAME = siteconf.DB_NAME
DB_USER = siteconf.DB_USER
ADMIN_EMAIL = site.mail_admin_email
ADMIN_PASSWORD = os.environ.get("VDB_ADMIN_PASSWORD")

# Nightly backups (script + systemd timer installed at the end of this deploy).
DB_CONTAINER = siteconf.DB_CONTAINER
BACKUP_SCRIPT = siteconf.BACKUP_SCRIPT
BACKUP_DIR = siteconf.BACKUP_DIR
RCLONE_REMOTE = site.backup_rclone_remote
# Crypt wrapper around RCLONE_REMOTE — dump contents and names are encrypted
# client-side before upload; Drive only sees ciphertext.
RCLONE_CRYPT_REMOTE = site.rclone_crypt_remote
RCLONE_DEST = site.rclone_dest
RCLONE_CONF = siteconf.RCLONE_CONF
BACKUP_RETAIN_LOCAL_DAYS = site.backup_retain_local_days
BACKUP_RETAIN_REMOTE_DAYS = site.backup_retain_remote_days
# Alerts for backup/drive-sync wrapper failures AND (as VDB_ALERT_EMAIL) for
# in-app scheduler job failures; "" disables the emails.
BACKUP_ALERT_EMAIL = site.mail_alert_email

# Nightly Drive roster sync (02:30 timer, AFTER the 02:00 backup: the dump is
# then a restore point taken right before the only automated bulk write). The
# sync uses the PLAIN Drive remote, not the crypt wrapper — leaders edit
# these sheets in the Drive UI. The remaining nightly jobs (page fetch,
# proposal digest, event reminders) run inside the app itself
# (volunteerdb.scheduler), so they need nothing from this deploy beyond the
# env file.
SHEETS_FOLDER = site.drive_sync_sheets_folder
SYNC_WORKDIR = siteconf.SYNC_WORKDIR
DRIVE_SYNC_SCRIPT = siteconf.DRIVE_SYNC_SCRIPT
DECORATE_SCRIPT = siteconf.DECORATE_SCRIPT
APP_UID = siteconf.APP_UID  # the image's `app` user; owns the sync workdir
REVOKE_PUBLIC_LINKS = site.drive_sync_revoke_public_links

# Host-side scheduling is plain systemd timer/service units (not quadlets —
# nothing container-scoped about them), installed into /etc/systemd/system.
SYSTEMD_DIR = siteconf.SYSTEMD_DIR
TIMER_UNITS = siteconf.TIMER_UNITS


# Reuse secrets already on the server; generate once if absent.
def _remote_env() -> dict[str, str]:
    lines = host.get_fact(FileContents, path=ENV_FILE) or []
    pairs = (
        line.split("=", 1)
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {k.strip(): v.strip() for k, v in pairs}


_existing = _remote_env()
storage_secret = _existing.get("VDB_STORAGE_SECRET") or pysecrets.token_hex(32)
db_password = _existing.get("VDB_DB_PASSWORD") or pysecrets.token_hex(24)
smtp2go_api_key = (
    os.environ.get("VDB_SMTP2GO_API_KEY") or _existing.get("VDB_SMTP2GO_API_KEY") or ""
)
if not smtp2go_api_key:
    print("NOTE: VDB_SMTP2GO_API_KEY not set - emails will be logged, not sent.")
# Set once (env var at deploy time or by hand in ENV_FILE) after the template
# sheet exists on Drive; reused on every later deploy like the secrets above.
template_sheet_url = (
    os.environ.get("VDB_TEMPLATE_SHEET_URL")
    or _existing.get("VDB_TEMPLATE_SHEET_URL")
    or ""
)
# Absolute origin for links in nightly-job emails (the event-reminder
# digest); defaults to the site's own domain.
public_base_url = (
    os.environ.get("VDB_PUBLIC_BASE_URL")
    or _existing.get("VDB_PUBLIC_BASE_URL")
    or site.public_base_url
)
database_url = (
    f"postgresql+asyncpg://{DB_USER}:{db_password}@{DB_CONTAINER}:5432/{DB_NAME}"
)

apt.packages(
    name="Install system packages",
    # aardvark-dns is only Recommends of netavark and the host sets
    # APT::Install-Recommends=false — without it, container-name DNS on the
    # volunteerdb network silently fails.
    packages=["podman", "aardvark-dns", "curl", "ca-certificates", "rclone"],
    update=True,
    cache_time=1440,
)
files.directory(name="App root", path="/opt/volunteerdb", mode="755")
files.directory(name="Config dir", path="/etc/volunteerdb", mode="755")
files.directory(name="Quadlet dir", path=QUADLET_DIR, mode="755")

files.sync(
    name="Sync application code (build context)",
    src=str(REPO_ROOT),
    dest=APP_DIR,
    delete=True,
    exclude=[  # fnmatch on FULL remote paths; applies to upload AND deletion
        "*.pyc",
        "*/.env",
        "*/.env.example",
        "*/.gitignore",
        "*/compose.yaml",
    ],
    exclude_dir=[  # traversal only — NOT applied to deletion
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
    ],
)
# Synced: src/, migrations/, docs/, alembic.ini, pyproject.toml, uv.lock,
#         README.md, .python-version, Containerfile, .containerignore

files.template(
    name="Write /etc/volunteerdb/env",
    src=str(HERE / "templates" / "env.j2"),
    dest=ENV_FILE,
    mode="600",
    user="root",
    group="root",
    database_url=database_url,
    storage_secret=storage_secret,
    smtp2go_api_key=smtp2go_api_key,
    db_password=db_password,
    port=APP_PORT,
    template_sheet_url=template_sheet_url,
    public_base_url=public_base_url,
    alert_email=BACKUP_ALERT_EMAIL,
)
files.template(
    name="Write /etc/volunteerdb/db.env",
    src=str(HERE / "templates" / "db.env.j2"),
    dest=DB_ENV_FILE,
    mode="600",
    user="root",
    group="root",
    db_password=db_password,
)

server.shell(
    name="Pull postgres image (if absent)",
    commands=[f"podman image exists {PG_IMAGE} || podman pull -q {PG_IMAGE}"],
)
server.shell(
    name="Build app image",
    commands=[f"podman build -t {IMAGE} {APP_DIR}"],
)

for _quadlet in QUADLETS:
    files.put(
        name=f"Install quadlet {_quadlet}",
        src=str(HERE / "files" / _quadlet),
        dest=f"{QUADLET_DIR}/{_quadlet}",
        mode="644",
    )
systemd.daemon_reload(name="daemon-reload (generate quadlet units)")

# Quadlet units reject `systemctl enable` — boot start comes from the
# [Install] section in the quadlet files, so never pass enabled= here.
# Notify=healthy in the db quadlet makes start block until pg_isready passes.
systemd.service(
    name="volunteerdb-db running",
    service="volunteerdb-db.service",
    running=True,
)

server.shell(
    name="alembic upgrade head (one-shot container)",
    commands=[
        f"podman run --rm --network {NET} --env-file {ENV_FILE} {IMAGE} alembic upgrade head"
    ],
)
if ADMIN_PASSWORD:
    # Bare -e flags pass the values through from the op's _env — the password
    # never appears in the command line or pyinfra logs.
    server.shell(
        name="Ensure admin user (one-shot container)",
        commands=[
            f"podman run --rm --network {NET} --env-file {ENV_FILE} "
            f"-e VDB_ADMIN_EMAIL -e VDB_ADMIN_PASSWORD {IMAGE} "
            "python -m volunteerdb.admin_bootstrap"
        ],
        _env={"VDB_ADMIN_EMAIL": ADMIN_EMAIL, "VDB_ADMIN_PASSWORD": ADMIN_PASSWORD},
    )
else:
    print("NOTE: VDB_ADMIN_PASSWORD not set - skipping admin bootstrap.")

systemd.service(
    name="volunteerdb-app running (restart onto fresh image)",
    service="volunteerdb-app.service",
    running=True,
    restarted=True,
)
server.shell(
    name="Wait for app + smoke test /login",
    commands=[
        "for i in $(seq 1 30); do "
        f'c=$(curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:{PORT}/login); '
        '[ "$c" = "200" ] && exit 0; sleep 1; done; '
        'echo "app did not serve /login with 200"; exit 1'
    ],
)

server.shell(
    name="Prune dangling images (bound build-layer growth)",
    commands=["podman image prune -f"],
)

# --- nightly backups: pg_dump -> gzip -> rclone crypt -> Google Drive --------
# Kept last so a missing rclone remote cannot block the app deploy (pyinfra
# runs ops in file order and stops at the first failure).
files.directory(name="Backup dir", path=BACKUP_DIR, mode="700")
files.template(
    name="Install backup script",
    src=str(HERE / "templates" / "volunteerdb-backup.sh.j2"),
    dest=BACKUP_SCRIPT,
    mode="700",
    user="root",
    group="root",
    db_container=DB_CONTAINER,
    db_user=DB_USER,
    db_name=DB_NAME,
    backup_dir=BACKUP_DIR,
    rclone_conf=RCLONE_CONF,
    rclone_dest=RCLONE_DEST,
    retain_local_days=BACKUP_RETAIN_LOCAL_DAYS,
    retain_remote_days=BACKUP_RETAIN_REMOTE_DAYS,
    alert_email=BACKUP_ALERT_EMAIL,
    env_file=ENV_FILE,
    mail_from=site.mail_from_address,
    mail_from_name=site.mail_from_name,
)
# Both rclone remotes (Drive + crypt wrapper) are provisioned ONCE by hand
# (docs/how-to/backup-restore.md): rclone rewrites the OAuth token inside
# rclone.conf on refresh, so this deploy must never write that file — it only
# asserts the remotes exist.
server.shell(
    name=f"Assert rclone remotes {RCLONE_REMOTE} + {RCLONE_CRYPT_REMOTE} are provisioned",
    commands=[
        f"for r in {RCLONE_REMOTE} {RCLONE_CRYPT_REMOTE}; do "
        f'grep -qxF "[$r]" {RCLONE_CONF} 2>/dev/null || '
        '{ echo "ERROR: rclone remote $r not configured on this host. '
        "Run the one-time Drive setup in docs/how-to/backup-restore.md, "
        'then re-run the deploy."; exit 1; }; done'
    ],
)
# --- nightly Drive roster sync -----------------------------------------------
# Same placement rationale as the backup block: a Drive problem must never
# block the app deploy. The sync must NOT go live before the one-time rclone
# round-trip verification in docs/how-to/drive-roster-sync.md has been run on
# this host (file ids must survive re-upload, or every shared link breaks).
files.directory(
    name="Drive sync work dir (writable by the container app user)",
    path=SYNC_WORKDIR,
    mode="750",
    user=str(APP_UID),
    group=str(APP_UID),
)
# Installed before the sync wrapper so the wrapper never references a
# missing binary. Sheet decoration (dropdowns, notes, hidden ID column)
# lives on the host because it reuses the rclone remote's OAuth client.
files.put(
    name="Install decorate-sheets script",
    src=str(HERE / "files" / "volunteerdb-decorate-sheets.py"),
    dest=DECORATE_SCRIPT,
    mode="700",
    user="root",
    group="root",
)
files.template(
    name="Install drive-sync script",
    src=str(HERE / "templates" / "volunteerdb-drive-sync.sh.j2"),
    dest=DRIVE_SYNC_SCRIPT,
    mode="700",
    user="root",
    group="root",
    rclone_conf=RCLONE_CONF,
    rclone_remote=RCLONE_REMOTE,
    sheets_folder=SHEETS_FOLDER,
    workdir=SYNC_WORKDIR,
    image=IMAGE,
    net=NET,
    env_file=ENV_FILE,
    decorate_script=DECORATE_SCRIPT,
    revoke_public_links=REVOKE_PUBLIC_LINKS,
    alert_email=BACKUP_ALERT_EMAIL,
    mail_from=site.mail_from_address,
    mail_from_name=site.mail_from_name,
)
# --- systemd timers for the two host-coupled jobs (crontab retired) ----------
# The backup (02:00) and Drive sync (02:30) stay host-side — rclone config,
# podman exec, and the /sync work dir live here — but fire from timer units
# instead of root's crontab. Units get their own journal names, so the
# wrappers no longer need systemd-cat.
server.shell(
    name="Assert host systemd supports tz-suffixed OnCalendar",
    commands=["systemd-analyze calendar '*-*-* 02:00:00 America/Toronto' >/dev/null"],
)
for _unit in TIMER_UNITS:
    files.put(
        name=f"Install unit {_unit}",
        src=str(HERE / "files" / _unit),
        dest=f"{SYSTEMD_DIR}/{_unit}",
        mode="644",
    )
systemd.daemon_reload(name="daemon-reload (timer units)")
systemd.service(
    name="volunteerdb-backup.timer enabled",
    service="volunteerdb-backup.timer",
    running=True,
    enabled=True,
)
systemd.service(
    name="volunteerdb-drive-sync.timer enabled",
    service="volunteerdb-drive-sync.timer",
    running=True,
    enabled=True,
)
