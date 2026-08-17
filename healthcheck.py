"""Container healthcheck: exit 0 iff the app serves /login.

Invoked by the quadlet HealthCmd as `python /app/healthcheck.py` — a plain
argv avoids quadlet's systemd-style quote lexing, which mangles inline
`python -c "..."` one-liners.
"""

import os
import sys
import urllib.request

# Deliberately does NOT import volunteerdb.config: a health probe that fails
# because settings will not parse cannot report anything useful, and this runs
# every 30s. The fallback duplicates config.py's default; the two are pinned
# together by tests/test_config_surface.py.
port = os.environ.get("VDB_PORT", "8080")
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=5)
except Exception as exc:
    sys.exit(f"unhealthy: {exc}")
