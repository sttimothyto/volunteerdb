"""Nightly two-way roster sync between the team sheets on Drive and the database.

Runs after the backup step, and its timer fires after the backup's, so the
dump is always a restore point taken immediately before the only automated
bulk write in the system.

Uses the PLAIN Drive remote rather than the crypt wrapper the backups go
through — leaders edit these sheets in the Drive UI, so they cannot be
ciphertext. The other nightly jobs (page fetch, proposal digest, event
reminders) run inside the app process and need nothing from this deploy
beyond the env file.
"""

import siteconf
from pyinfra.api.deploy import deploy
from pyinfra.operations import files

from . import units


@deploy("Drive roster sync")
def deploy_drive_sync(site, *, here, unit_vars) -> None:
    files.directory(
        name="Drive sync work dir (writable by the container app user)",
        path=siteconf.SYNC_WORKDIR,
        mode="750",
        user=str(siteconf.APP_UID),
        group=str(siteconf.APP_UID),
    )
    # Installed before the sync wrapper so the wrapper never references a
    # missing binary. Sheet decoration (dropdowns, notes, hidden ID column)
    # lives on the host because it reuses the rclone remote's OAuth client.
    files.put(
        name="Install decorate-sheets script",
        src=str(here / "files" / "volunteerdb-decorate-sheets.py"),
        dest=siteconf.DECORATE_SCRIPT,
        mode="700",
        user="root",
        group="root",
    )
    files.template(
        name="Install drive-sync script",
        src=str(here / "templates" / "volunteerdb-drive-sync.sh.j2"),
        dest=siteconf.DRIVE_SYNC_SCRIPT,
        mode="700",
        user="root",
        group="root",
        rclone_conf=siteconf.RCLONE_CONF,
        rclone_remote=site.backup_rclone_remote,
        sheets_folder=site.drive_sync_sheets_folder,
        workdir=siteconf.SYNC_WORKDIR,
        image=siteconf.IMAGE,
        net=siteconf.NET,
        env_file=siteconf.ENV_FILE,
        app_uid=siteconf.APP_UID,
        decorate_script=siteconf.DECORATE_SCRIPT,
        revoke_public_links=site.drive_sync_revoke_public_links,
        alert_email=site.mail_alert_email,
        mail_from=site.mail_from_address,
        mail_from_name=site.mail_from_name,
    )
    # The sync must NOT go live before the one-time rclone round-trip check in
    # docs/how-to/drive-roster-sync.md has been run on this host: file ids
    # must survive re-upload, or every shared link breaks.
    units.install_timer(
        here,
        unit_vars,
        units=("volunteerdb-drive-sync.service", "volunteerdb-drive-sync.timer"),
        timer="volunteerdb-drive-sync.timer",
        reload_name="daemon-reload (drive-sync timer)",
    )
