"""
Deploy VolunteerDB to sttimothyto-prod as a containerized stack (podman Quadlet).

Usage (from the pyinfra checkout):
    cd ~/pyinfra
    uv run pyinfra sttimothyto-prod /home/ben/volunteerdb/deploy/deploy.py --dry
    VDB_ADMIN_PASSWORD='...' uv run pyinfra sttimothyto-prod /home/ben/volunteerdb/deploy/deploy.py

Outbound email (SMTP2GO): pass VDB_SMTP2GO_API_KEY='api-...' on the first
deploy after the mail feature; it is read back from the remote env file on
later runs (env var wins, so passing it again rotates the key).

Architecture — one process per container:
  volunteerdb-db.service   quadlet: docker.io/library/postgres:17, named volume
                           volunteerdb-pgdata. No published port; reachable only
                           on the "volunteerdb" podman network as volunteerdb-db.
  volunteerdb-app.service  quadlet: localhost/volunteerdb:latest, built on the
                           server from the synced source (APP_DIR is the build
                           context). Publishes 127.0.0.1:8090 -> 8080.
  Migrations and admin bootstrap run as one-shot `podman run --rm` containers
  on the same network, using the same image and env file.

CUTOVER (runs once, guarded on host postgresql@17-main still being active):
  stop old native volunteerdb.service -> pg_dump host DB to DUMP -> start
  container DB (fresh initdb from db.env, reusing the existing password) ->
  restore dump (single transaction; skipped if the container DB is not empty)
  -> and only after the smoke test passes: stop host postgres, set its cluster
  to manual start, retire the old unit file to
  /opt/volunteerdb/volunteerdb.service.native-backup.
  Take a manual backup first:
      ssh sttimothyto-prod \\
        'su - postgres -c "pg_dump volunteerdb" > /root/volunteerdb-manual-backup.sql'

ROLLBACK (while rollback assets exist):
  systemctl stop volunteerdb-app volunteerdb-db
  systemctl start postgresql@17-main        # data dir untouched by cutover
  # carry back post-cutover writes if any:
  #   podman start volunteerdb-db && podman exec volunteerdb-db \\
  #     pg_dump --no-owner -U volunteerdb volunteerdb | su - postgres -c 'psql volunteerdb'
  # then `git checkout <pre-container commit> -- deploy/` and re-run pyinfra
  # (idempotent, reuses secrets), or restore the backed-up unit file and
  # hand-edit /etc/volunteerdb/env back to 127.0.0.1:5432 / VDB_HOST=127.0.0.1
  # / VDB_PORT=8090.

MANUAL CLEANUP after burn-in (intentionally not automated):
  /opt/volunteerdb/venv, /var/lib/volunteerdb,
  /opt/volunteerdb/volunteerdb.service.native-backup,
  /var/backups/volunteerdb-cutover.sql,
  apt remove postgresql* and /var/lib/postgresql (old cluster data)

MANUAL POST-DEPLOY STEPS (unchanged):
  1. DNS: add A record  vdb.sttimothyto.org -> 91.99.133.210
  2. /etc/caddy/Caddyfile already proxies vdb.sttimothyto.org -> 127.0.0.1:8090
  3. Backups: one-time rclone Google Drive remote provisioning (headless OAuth
     paste flow) — runbook in docs/how-to/backup-restore.md. Never manage
     /root/.config/rclone/rclone.conf from this script: rclone rewrites the
     token in place on refresh, and a deploy would clobber it.
"""

import os
import secrets as pysecrets
from pathlib import Path

from pyinfra import host
from pyinfra.facts.files import File, FileContents
from pyinfra.facts.systemd import SystemdStatus
from pyinfra.operations import apt, crontab, files, server, systemd

# pyinfra resolves relative src paths against the invocation cwd, not this
# file — anchor everything here so the deploy works from any directory.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

APP_DIR = "/opt/volunteerdb/app"  # synced source == podman build context
ENV_FILE = "/etc/volunteerdb/env"
DB_ENV_FILE = "/etc/volunteerdb/db.env"
QUADLET_DIR = "/etc/containers/systemd"
QUADLETS = ("volunteerdb.network", "volunteerdb-db.container", "volunteerdb-app.container")
IMAGE = "localhost/volunteerdb:latest"
PG_IMAGE = "docker.io/library/postgres:17"
NET = "volunteerdb"
PORT = 8090  # host loopback port Caddy proxies to (PublishPort in the app quadlet)
APP_PORT = 8080  # port inside the app container
DB_NAME = DB_USER = "volunteerdb"
DUMP = "/var/backups/volunteerdb-cutover.sql"
OLD_UNIT = "/etc/systemd/system/volunteerdb.service"
ADMIN_EMAIL = "admin@sttimothyto.org"
ADMIN_PASSWORD = os.environ.get("VDB_ADMIN_PASSWORD")

# Nightly backups (script + crontab installed at the end of this deploy).
DB_CONTAINER = "volunteerdb-db"
BACKUP_SCRIPT = "/usr/local/bin/volunteerdb-backup"
BACKUP_DIR = "/var/backups/volunteerdb"
RCLONE_REMOTE = "volunteerdb-gdrive-backup"
RCLONE_DEST = f"{RCLONE_REMOTE}:volunteerdb-backups"
RCLONE_CONF = "/root/.config/rclone/rclone.conf"
BACKUP_RETAIN_LOCAL_DAYS = 14
BACKUP_RETAIN_REMOTE_DAYS = 730
BACKUP_ALERT_EMAIL = ADMIN_EMAIL  # "" disables the failure email


# Reuse secrets already on the server; generate once if absent.
def _remote_env() -> dict[str, str]:
    lines = host.get_fact(FileContents, path=ENV_FILE) or []
    pairs = (l.split("=", 1) for l in lines if "=" in l and not l.lstrip().startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


_existing = _remote_env()
storage_secret = _existing.get("VDB_STORAGE_SECRET") or pysecrets.token_hex(32)
db_password = _existing.get("VDB_DB_PASSWORD") or pysecrets.token_hex(24)
smtp2go_api_key = (
    os.environ.get("VDB_SMTP2GO_API_KEY") or _existing.get("VDB_SMTP2GO_API_KEY") or ""
)
if not smtp2go_api_key:
    print("NOTE: VDB_SMTP2GO_API_KEY not set - emails will be logged, not sent.")
database_url = f"postgresql+asyncpg://{DB_USER}:{db_password}@volunteerdb-db:5432/{DB_NAME}"

# First containerized run against a host still serving the native deploy?
_units = host.get_fact(SystemdStatus) or {}
CUTOVER = bool(_units.get("postgresql@17-main.service"))
OLD_UNIT_PRESENT = host.get_fact(File, path=OLD_UNIT) is not None

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
        "*/create_admin.py",  # uploaded by files.put below; keep delete=True off it
    ],
    exclude_dir=[  # traversal only — NOT applied to deletion
        ".git",
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

files.put(
    name="Upload create_admin.py (baked into the image)",
    src=str(HERE / "files" / "create_admin.py"),
    dest=f"{APP_DIR}/create_admin.py",
    mode="644",
)
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

# --- cutover A: freeze the native stack and dump its data -------------------
if CUTOVER:
    if OLD_UNIT_PRESENT:
        systemd.service(
            name="Cutover: stop old native app",
            service="volunteerdb",
            running=False,
        )
    server.shell(
        name="Cutover: dump host database",
        commands=[
            f"install -m 600 /dev/null {DUMP} && "
            f"su - postgres -c 'pg_dump --no-owner --no-privileges {DB_NAME}' > {DUMP}"
        ],
    )

# Quadlet units reject `systemctl enable` — boot start comes from the
# [Install] section in the quadlet files, so never pass enabled= here.
# Notify=healthy in the db quadlet makes start block until pg_isready passes.
systemd.service(
    name="volunteerdb-db running",
    service="volunteerdb-db.service",
    running=True,
)

# --- cutover B: restore the dump into the (empty) container DB --------------
if CUTOVER:
    _guard = (
        f'test -z "$(podman exec volunteerdb-db psql -U {DB_USER} -d {DB_NAME}'
        ' -tAc "SELECT to_regclass(\'public.alembic_version\')")"'
    )
    server.shell(
        name="Cutover: restore dump into container DB (skips if not empty)",
        commands=[
            f"if {_guard}; then "
            f"podman exec -i volunteerdb-db psql -q -1 -v ON_ERROR_STOP=1 "
            f"-U {DB_USER} -d {DB_NAME} < {DUMP}; "
            'else echo "container DB not empty - skipping restore"; fi'
        ],
    )

server.shell(
    name="alembic upgrade head (one-shot container)",
    commands=[f"podman run --rm --network {NET} --env-file {ENV_FILE} {IMAGE} alembic upgrade head"],
)
if ADMIN_PASSWORD:
    # Bare -e flags pass the values through from the op's _env — the password
    # never appears in the command line or pyinfra logs.
    server.shell(
        name="Ensure admin user (one-shot container)",
        commands=[
            f"podman run --rm --network {NET} --env-file {ENV_FILE} "
            f"-e VDB_ADMIN_EMAIL -e VDB_ADMIN_PASSWORD {IMAGE} python /app/create_admin.py"
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

# --- cutover C: only after the smoke test — retire the native stack ---------
if CUTOVER:
    server.shell(
        name="Cutover: stop host postgres + prevent autostart",
        commands=[
            "systemctl stop postgresql@17-main postgresql",
            # Debian's generator re-enables clusters from start.conf, so flip
            # it to manual (still startable by hand for rollback).
            "sed -i 's/^auto/manual/' /etc/postgresql/17/main/start.conf",
            "systemctl disable postgresql@17-main 2>/dev/null || true",
        ],
    )
    if OLD_UNIT_PRESENT:
        server.shell(
            name="Cutover: retire old native unit",
            commands=[
                "systemctl disable volunteerdb 2>/dev/null || true",
                f"mv {OLD_UNIT} /opt/volunteerdb/volunteerdb.service.native-backup",
                "systemctl daemon-reload",
            ],
        )

server.shell(
    name="Prune dangling images (bound build-layer growth)",
    commands=["podman image prune -f"],
)

# --- nightly backups: pg_dump -> gzip -> Google Drive via rclone -------------
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
    mail_from="no-reply@sttimothyto.org",
    mail_from_name="VolunteerDB",
)
# The rclone remote is provisioned ONCE by hand (docs/how-to/backup-restore.md):
# rclone rewrites the OAuth token inside rclone.conf on refresh, so this deploy
# must never write that file — it only asserts the remote exists.
server.shell(
    name=f"Assert rclone remote {RCLONE_REMOTE} is provisioned",
    commands=[
        f"grep -qx '\\[{RCLONE_REMOTE}\\]' {RCLONE_CONF} 2>/dev/null || "
        f"{{ echo 'ERROR: rclone remote {RCLONE_REMOTE} not configured on this host. "
        "Run the one-time Drive setup in docs/how-to/backup-restore.md, "
        "then re-run the deploy.'; exit 1; }"
    ],
)
crontab.crontab(
    name="Nightly backup crontab entry (02:00 America/Toronto)",
    command=f"systemd-cat -t volunteerdb-backup {BACKUP_SCRIPT}",
    cron_name="volunteerdb-backup",
    minute="0",
    hour="2",
    user="root",
)
