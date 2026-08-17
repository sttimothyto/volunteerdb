"""System packages and the directories everything else writes into."""

import siteconf
from pyinfra.api.deploy import deploy
from pyinfra.operations import apt, files


@deploy("System packages and directories")
def install_base() -> None:
    apt.packages(
        name="Install system packages",
        # aardvark-dns is only Recommends of netavark and the host sets
        # APT::Install-Recommends=false — without it, container-name DNS on
        # the volunteerdb network silently fails.
        packages=["podman", "aardvark-dns", "curl", "ca-certificates", "rclone"],
        update=True,
        cache_time=1440,
    )
    files.directory(name="App root", path="/opt/volunteerdb", mode="755")
    files.directory(name="Config dir", path="/etc/volunteerdb", mode="755")
    files.directory(name="Quadlet dir", path=siteconf.QUADLET_DIR, mode="755")
