"""
Deploy VolunteerDB as a containerized stack (podman Quadlet under systemd).

Idempotent: the same command performs a first install and a routine upgrade.

Usage — VDB_SITE names a file in deploy/sites/, which holds everything
specific to that parish; deploy/siteconf.py holds what is the same on all of
them. The inventory reads the host from the same file:

    make deploy-dry SITE=<your-site>
    VDB_ADMIN_PASSWORD='...' make deploy SITE=<your-site>

(`make deploy` is `VDB_SITE=<site> uvx pyinfra==<pin> deploy/inventory.py
deploy/deploy.py -y`; the pin lives in the Makefile and in CI.)

Optional environment: VDB_ADMIN_PASSWORD runs the admin bootstrap (required
on a brand-new instance — it is the only way in); VDB_SMTP2GO_API_KEY sets or
rotates the mail key. Both are read back from the remote env file on later
runs, so passing them again is what rotates them.

This file is the running order and nothing else; each step lives in
deploy/steps/. Standing up a NEW instance is docs/how-to/new-instance.md —
this script is only the part that is automated. Why it is shaped this way
(quadlets, the build on the server, self-managing secrets) is
docs/explanation/deployment.md; how to run and roll it back is
docs/how-to/deploy.md.
"""

import os
import sys
from pathlib import Path

# pyinfra resolves relative src paths against the invocation cwd, not this
# file — anchor everything here so the deploy works from any directory. It
# also execs this file rather than importing it, so HERE has to go on
# sys.path before siteconf and steps can be imported.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import siteconf  # noqa: E402  (must follow the sys.path line above)
from steps import secrets as secrets_step  # noqa: E402
from steps.app import deploy_app  # noqa: E402
from steps.backup import deploy_backup  # noqa: E402
from steps.base import install_base  # noqa: E402
from steps.proxy import deploy_proxy  # noqa: E402

# Everything specific to this parish; everything else is a siteconf constant.
site = siteconf.load()

# Every quadlet and timer/service unit renders from this one dict, so a value
# cannot reach one unit and miss another — the ports in the app quadlet and
# the smoke test, or the timezone in the two timers and the calendar
# assertion, have to agree or the stack half-works.
UNIT_VARS = dict(
    net=siteconf.NET,
    image=siteconf.IMAGE,
    pg_image=siteconf.PG_IMAGE,
    pg_tuning=siteconf.PG_TUNING,
    db_container=siteconf.DB_CONTAINER,
    db_user=siteconf.DB_USER,
    db_name=siteconf.DB_NAME,
    env_file=siteconf.ENV_FILE,
    db_env_file=siteconf.DB_ENV_FILE,
    listen_port=site.host_listen_port,
    app_port=siteconf.APP_PORT,
    backup_script=siteconf.BACKUP_SCRIPT,
    timezone=site.site_timezone,
    backup_at=site.schedule_backup_at,
)

# Reads /etc/volunteerdb/env back off the host, so this runs per host and
# before any operation that needs a secret.
secrets = secrets_step.resolve(
    site,
    env_file=siteconf.ENV_FILE,
    db_user=siteconf.DB_USER,
    db_name=siteconf.DB_NAME,
    db_host=siteconf.DB_CONTAINER,
)

# --- the deploy, in order ----------------------------------------------------
# The steps are CALLED, not imported for their side effects: pyinfra execs
# this file once per host, and an imported module's body would run only for
# the first of them. See deploy/steps/__init__.py.
#
# The proxy follows the app, so Caddy's first reload finds something answering
# on the loopback port. The backup comes last so that a missing rclone remote
# cannot block either — pyinfra stops at the first failure, and on a fresh
# instance the rclone assertion is expected to fail until the one-time
# provisioning is done. By then the app is up and, if the site file asks for
# it, so is TLS.

install_base()

deploy_app(
    site,
    secrets,
    here=HERE,
    repo_root=REPO_ROOT,
    unit_vars=UNIT_VARS,
    admin_password=os.environ.get("VDB_ADMIN_PASSWORD"),
)

deploy_proxy(site, here=HERE)

deploy_backup(site, here=HERE, unit_vars=UNIT_VARS)
