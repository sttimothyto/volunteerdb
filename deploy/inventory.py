"""pyinfra inventory: the one host named by $VDB_SITE's file.

    VDB_SITE=sttimothy uvx pyinfra deploy/inventory.py deploy/deploy.py --dry

Kept separate from deploy.py because pyinfra loads the inventory first and
independently. DEPLOY_HOST overrides the site file's ssh_host, which is how CI
can target a host without one being committed.
"""

import os
import sys
from pathlib import Path

# pyinfra execs this file rather than importing it, so its directory is not on
# sys.path and `import siteconf` would fail without this.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import siteconf  # noqa: E402  (must follow the sys.path line above)

_site = siteconf.load()

hosts = [
    (
        os.environ.get("DEPLOY_HOST") or _site.host_ssh_host,
        {"ssh_user": _site.host_ssh_user},
    )
]
