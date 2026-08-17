"""Nightly backup: pg_dump -> gzip -> rclone crypt -> Google Drive.

Called after the application stack so that a Drive problem can never block the
app deploy: pyinfra runs operations in order and stops at the first failure,
and on a fresh instance the rclone assertion below is *expected* to fail until
the one-time provisioning has been done. By then the app itself is up.
"""

import siteconf
from pyinfra.api.deploy import deploy
from pyinfra.operations import files, server

from . import units


@deploy("Nightly backup")
def deploy_backup(site, *, here, unit_vars) -> None:
    files.directory(name="Backup dir", path=siteconf.BACKUP_DIR, mode="700")
    files.template(
        name="Install backup script",
        src=str(here / "templates" / "volunteerdb-backup.sh.j2"),
        dest=siteconf.BACKUP_SCRIPT,
        mode="700",
        user="root",
        group="root",
        db_container=siteconf.DB_CONTAINER,
        db_user=siteconf.DB_USER,
        db_name=siteconf.DB_NAME,
        backup_dir=siteconf.BACKUP_DIR,
        rclone_conf=siteconf.RCLONE_CONF,
        rclone_dest=site.rclone_dest,
        retain_local_days=site.backup_retain_local_days,
        retain_remote_days=site.backup_retain_remote_days,
        alert_email=site.mail_alert_email,
        env_file=siteconf.ENV_FILE,
        mail_from=site.mail_from_address,
        mail_from_name=site.mail_from_name,
    )
    # Both rclone remotes (Drive + crypt wrapper) are provisioned ONCE by hand
    # (docs/how-to/backup-restore.md). rclone rewrites the OAuth token inside
    # rclone.conf whenever it refreshes, so this deploy must NEVER write that
    # file — templating it would clobber a live token. It only asserts the
    # remotes exist.
    plain, crypt = site.backup_rclone_remote, site.rclone_crypt_remote
    server.shell(
        name=f"Assert rclone remotes {plain} + {crypt} are provisioned",
        commands=[
            f"for r in {plain} {crypt}; do "
            f'grep -qxF "[$r]" {siteconf.RCLONE_CONF} 2>/dev/null || '
            '{ echo "ERROR: rclone remote $r not configured on this host. '
            "Run the one-time Drive setup in docs/how-to/backup-restore.md, "
            'then re-run the deploy."; exit 1; }; done'
        ],
    )
    # Checked once, here, because it is a property of the host rather than of
    # either timer: tz-suffixed OnCalendar needs systemd >= 235, and without
    # it both timers would fire on UTC instead of parish time.
    server.shell(
        name="Assert host systemd supports tz-suffixed OnCalendar",
        commands=[
            f"systemd-analyze calendar "
            f"'*-*-* {site.schedule_backup_at}:00 {site.site_timezone}' >/dev/null"
        ],
    )
    units.install_timer(
        here,
        unit_vars,
        units=("volunteerdb-backup.service", "volunteerdb-backup.timer"),
        timer="volunteerdb-backup.timer",
        reload_name="daemon-reload (backup timer)",
    )
