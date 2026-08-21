"""The site logo, served to anyone — including signed-out browsers.

Unlike /photos/{id} this is deliberately public: the login page and the public
ministries shell both show it, and neither has a session. Registered under
/logo, which main.py exempts from AuthMiddleware.

**Revalidated, not cached for a fixed period.** The obvious design — a long
max-age plus a ?v= cache-buster in the URL — cannot work here, because
layout.frame() is a synchronous contextmanager with no database session and so
cannot look up a version to put in the URL. A flat max-age instead is worse
than it looks: ui.navigate.reload() after an upload does not bypass the
browser's cache for a subresource, so the admin who just uploaded a logo would
keep seeing the old one until the lifetime expired.

So: `no-cache` (store it, but check before using it) plus an ETag. Browsers
revalidate on each page and get a 304 with no body almost every time, uploads
appear immediately, and the 304 path never loads the image bytes at all — the
ETag comes from uploaded_at, which is a far smaller read than the blob.
"""

import hashlib
from functools import lru_cache

import sqlalchemy as sa
from fastapi import Request
from nicegui import app
from starlette.responses import Response

from ..db import db_session
from ..models import SiteLogo
from ..services import branding
from .assets import STATIC_DIR

# "store this, but ask me before you use it" — see the module docstring
CACHE_HEADERS = {"Cache-Control": "no-cache"}
PLACEHOLDER = "logo-placeholder.svg"


@lru_cache
def _placeholder() -> bytes:
    """Read once per process — the file ships in the image and never changes
    under a running app."""
    return (STATIC_DIR / PLACEHOLDER).read_bytes()


@lru_cache
def _placeholder_etag() -> str:
    return f'"p{hashlib.md5(_placeholder()).hexdigest()[:16]}"'


def register() -> None:
    """Wire the route onto the global app from register_pages(), for the reason
    ministries_routes.register() gives: test simulations wipe the app's routes
    and re-run create_app() with this module already imported, so an
    import-time decorator would fire only once per process."""
    app.get("/logo", include_in_schema=False)(logo)


async def logo(request: Request) -> Response:
    async with db_session() as session:
        # just the timestamp: enough to answer a revalidation without ever
        # reading the blob, which is the common case on every page load
        stamp = (
            await session.execute(
                sa.select(SiteLogo.uploaded_at).where(SiteLogo.id == branding.ROW_ID)
            )
        ).scalar_one_or_none()

    etag = _placeholder_etag() if stamp is None else f'"l{stamp.timestamp():.6f}"'
    headers = {**CACHE_HEADERS, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    if stamp is None:
        return Response(
            content=_placeholder(), media_type="image/svg+xml", headers=headers
        )
    async with db_session() as session:
        record = await branding.get(session)
    if record is None:  # deleted between the two reads; serve the fallback
        return Response(
            content=_placeholder(),
            media_type="image/svg+xml",
            headers={**CACHE_HEADERS, "ETag": _placeholder_etag()},
        )
    return Response(
        content=record.image, media_type=record.content_type, headers=headers
    )
