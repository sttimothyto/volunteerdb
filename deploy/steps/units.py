"""Shared by the two host-side timer steps.

The backup and the Drive sync stay host-side — rclone config, podman exec and
the /sync work dir all live here — but fire from systemd timers rather than
cron. Units get their own journal names, so the wrappers need no systemd-cat.
"""

import siteconf
from pyinfra.operations import files, systemd


def install_timer(here, unit_vars, *, units, timer, reload_name) -> None:
    """Render a .service/.timer pair, reload, and enable the timer."""
    for unit in units:
        files.template(
            name=f"Install unit {unit}",
            src=str(here / "templates" / f"{unit}.j2"),
            dest=f"{siteconf.SYSTEMD_DIR}/{unit}",
            mode="644",
            **unit_vars,
        )
    systemd.daemon_reload(name=reload_name)
    systemd.service(name=f"{timer} enabled", service=timer, running=True, enabled=True)
