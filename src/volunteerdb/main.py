import logging
import secrets
from pathlib import Path
from urllib.parse import quote

from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .api import api_router
from .api.deps import install_exception_handlers
from .config import settings
from .ui import register_pages
from .ui.context import session_user_id

UNRESTRICTED_PREFIXES = ("/login", "/invite/", "/api/", "/_nicegui", "/static/", "/favicon")
ASSET_PREFIXES = ("/_nicegui", "/static/", "/favicon", "/api/")

# Cookie inactivity bound. Real session lifetime is enforced app-side via
# session_expires_at (see ui/context.py); 92 days covers every case where the
# 90-day "keep me signed in" window could still be valid.
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 92


class AuthMiddleware(BaseHTTPMiddleware):
    """Send anonymous browsers to /login. The JSON API under /api authenticates
    itself with Bearer tokens and is exempt here."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if "id" in request.session and not any(path.startswith(p) for p in ASSET_PREFIXES):
            # Starlette only re-issues the session cookie when the session dict
            # was modified, so without this touch it would hard-expire max_age
            # after the FIRST visit, even for active users.
            request.session["id"] = request.session["id"]
        if not any(path.startswith(p) for p in UNRESTRICTED_PREFIXES):
            if session_user_id() is None:
                return RedirectResponse(f"/login?redirect_to={quote(path)}")
        return await call_next(request)


def create_app() -> None:
    install_exception_handlers(app)
    app.include_router(api_router)
    app.add_static_files("/static", str(Path(__file__).parent / "ui" / "static"))
    app.colors(
        primary="#A5573E",
        secondary="#8A7550",
        accent="#B08D57",
        dark="#2A2622",
        dark_page="#1C1917",
        positive="#5F7A3D",
        negative="#9B3B30",
        warning="#B07D2B",
        info="#4C7086",
    )
    ui.add_head_html(
        '<link rel="preload" href="/static/fonts/cinzel-v11-latin-regular.woff2" '
        'as="font" type="font/woff2" crossorigin>',
        shared=True,
    )
    ui.add_head_html('<link rel="stylesheet" href="/static/theme.css">', shared=True)
    app.add_middleware(AuthMiddleware)
    register_pages()


def run() -> None:
    create_app()
    s = settings()
    secret = s.storage_secret
    if not secret or secret in ("dev-secret-change-me", "change-me-to-a-long-random-string"):
        secret = secrets.token_hex(32)  # never serve forgeable sessions
        logging.getLogger(__name__).warning(
            "VDB_STORAGE_SECRET unset — using an ephemeral secret; sessions reset on restart"
        )
    ui.run(
        host=s.host,
        port=s.port,
        title="VolunteerDB",
        favicon="⛪",
        storage_secret=secret,
        session_middleware_kwargs={
            "max_age": SESSION_COOKIE_MAX_AGE,
            "https_only": s.cookie_secure,
        },
        reload=s.reload,
        show=False,
        fastapi_docs=True,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
