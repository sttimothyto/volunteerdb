"""Content-hashed URLs for /static assets, so the long Cache-Control
lifetime set in main.py survives deploys: new content -> new ?v= -> cache
miss. Hash the bytes rather than mtime — image rebuilds refresh mtimes on
every deploy even when the files are unchanged."""

import hashlib
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"


@lru_cache
def static_url(name: str) -> str:
    digest = hashlib.md5((STATIC_DIR / name).read_bytes()).hexdigest()[:8]
    return f"/static/{name}?v={digest}"
