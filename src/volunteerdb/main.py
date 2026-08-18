import secrets
import time
from pathlib import Path
from urllib.parse import quote

import structlog
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from . import scheduler
from .api import api_router
from .api.deps import install_exception_handlers
from .config import settings
from .log import init_logging
from .ui import register_pages
from .ui.assets import static_url
from .ui.context import session_user_id

logger = structlog.get_logger(__name__)

UNRESTRICTED_PREFIXES = (
    "/login",
    "/invite/",
    # the link mailed to a new address: opened days later, from whatever
    # browser reads that mailbox (ui/login.py: confirm_email_page)
    "/confirm-email/",
    "/api/",
    "/_nicegui",
    "/static/",
    "/favicon",
    "/ministries",  # public ministry home pages (ui/ministries_routes.py)
)
# /photos/ is cookie-authed but asset-like: skipping the session re-issue keeps
# its long-lived Cache-Control effective (a Set-Cookie per image defeats caching)
ASSET_PREFIXES = (
    "/_nicegui",
    "/static/",
    "/favicon",
    "/api/",
    "/photos/",
    "/ministries/img/",
)
# Request-log lines for these drop to DEBUG (unlike ASSET_PREFIXES, /api/ stays
# at INFO — API calls are exactly the traffic worth seeing).
QUIET_PREFIXES = ("/_nicegui", "/static/", "/favicon", "/photos/", "/ministries/img/")

# Paths whose LAST segment is a bearer credential. The request line records the
# path, so at INFO these would write live invite and address-change links into
# the log — and into any reverse-proxy access log in front of it. The prefix is
# kept (it is the useful half: which flow was exercised) and the token is
# replaced. Redaction rather than QUIET_PREFIXES: a redemption attempt is
# exactly the traffic worth seeing, it is just the token that must not be in it.
SECRET_PATH_PREFIXES = ("/invite/", "/confirm-email/")


def redact_path(path: str) -> str:
    for prefix in SECRET_PATH_PREFIXES:
        if path.startswith(prefix):
            return f"{prefix}<redacted>"
    return path


# Cookie inactivity bound. Real session lifetime is enforced app-side via
# session_expires_at (see ui/context.py); 92 days covers every case where the
# 90-day "keep me signed in" window could still be valid.
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 92


# The paths a browser authenticates *through*. Loading one of these while
# anonymous mints a fresh session id, which is the fixation defence: NiceGUI
# assigns the session id on the first request and never changes it, and
# app.storage.user is keyed by it — so an attacker who can plant the cookie (a
# shared kiosk, a sibling-subdomain XSS, an active network attacker where
# VDB_COOKIE_SECURE is off) could otherwise fix a known id and inherit whatever
# the victim signs into it.
#
# Rotating HERE rather than in establish_session is not a detail: sign-in
# happens over the websocket, where there is no HTTP response left to carry a
# Set-Cookie, so a new id assigned at that moment would only ever move the
# server's bucket out from under the browser. On the way IN there is a real
# response, and the id the victim then authenticates into is one the server
# minted after the planted cookie was discarded.
AUTH_ENTRY_PREFIXES = ("/login", "/invite/", "/confirm-email/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Send anonymous browsers to /login. The JSON API under /api authenticates
    itself with Bearer tokens and is exempt here."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in AUTH_ENTRY_PREFIXES):
            await self._rotate_anonymous_session(request)
        if "id" in request.session and not any(
            path.startswith(p) for p in ASSET_PREFIXES
        ):
            # Starlette only re-issues the session cookie when the session dict
            # was modified, so without this touch it would hard-expire max_age
            # after the FIRST visit, even for active users.
            request.session["id"] = request.session["id"]
        if not any(path.startswith(p) for p in UNRESTRICTED_PREFIXES):
            if session_user_id() is None:
                return RedirectResponse(f"/login?redirect_to={quote(path)}")
        return await call_next(request)

    @staticmethod
    async def _rotate_anonymous_session(request: Request) -> None:
        """Give an anonymous browser a fresh session id (see
        AUTH_ENTRY_PREFIXES). Only anonymous ones: a signed-in reader who
        wanders onto /login must keep the session they already have.

        Only browsers that turned up *holding* a session cookie: a first-time
        visitor was handed a fresh id by NiceGUI on this very request, so there
        is nothing to displace, and rotating anyway would mint a storage file
        per anonymous hit — /login is exactly the path crawlers pound.

        The dark-mode preference rides along — that is how this browser likes
        to read, whoever is using it — and nothing else, because an id somebody
        else chose should contribute no state at all. The bucket left behind is
        discarded with it, so rotation moves a session rather than accumulating
        one. Reaches into nicegui.storage for want of a public API;
        test_app_surface pins the behaviour so a NiceGUI change fails loudly
        instead of quietly dropping the protection."""
        if "session" not in request.cookies:
            return
        try:
            if session_user_id() is not None:
                return
            dark_mode = app.storage.user.get("dark_mode")
        except Exception:  # no storage context yet: nothing to rotate
            return
        stale_id = request.session.get("id")
        request.session["id"] = secrets.token_urlsafe(24)
        await app.storage._create_user_storage(request.session["id"])
        if dark_mode is not None:
            app.storage.user["dark_mode"] = dark_mode
        if stale_id is not None:
            stale = app.storage._users.pop(stale_id, None)
            filepath = getattr(stale, "filepath", None)
            if filepath is not None:
                filepath.unlink(missing_ok=True)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """One line per request: method, path, status, duration — with the cookie
    user id bound so downstream lines (and this one) carry identity. The API
    dependency and page_session rebind the full id:email once the actor loads."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        logged_path = redact_path(path)
        ip = request.client.host if request.client else "-"
        via = "api" if path.startswith("/api/") else "gui"
        try:
            user = str(session_user_id() or "-")
        except Exception:  # outside NiceGUI storage context
            user = "-"
        start = time.perf_counter()
        with structlog.contextvars.bound_contextvars(user=user, ip=ip, via=via):
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "http.request_failed", method=request.method, path=logged_path
                )
                raise
            line = (
                logger.debug
                if any(path.startswith(p) for p in QUIET_PREFIXES)
                else logger.info
            )
            line(
                "http.request",
                method=request.method,
                path=logged_path,
                status=response.status_code,
                ms=round((time.perf_counter() - start) * 1000),
            )
            return response


def create_app() -> None:
    init_logging()
    install_exception_handlers(app)
    app.include_router(api_router)
    # 30 days, safe because mutable assets are referenced via assets.static_url
    # (?v=<content hash>); the default is only an hour
    app.add_static_files(
        "/static",
        str(Path(__file__).parent / "ui" / "static"),
        max_cache_age=30 * 24 * 3600,
    )
    # Built Sphinx manual. Deliberately NOT in UNRESTRICTED_PREFIXES — but
    # "signed in" here means every account, volunteers included, not just
    # admins: the manual is the app's own help and splitting it would make
    # ordinary pages cross-link into 403s. The ops pages (secret rotation,
    # backups, deploy) therefore reach every signed-in reader. That is
    # accepted: they describe procedure, never credentials.
    docs_dir = Path(settings().docs_dir)
    if docs_dir.is_dir():
        app.mount("/manual", StaticFiles(directory=docs_dir, html=True), name="manual")
    else:

        @app.get("/manual", include_in_schema=False)
        def _manual_not_built() -> PlainTextResponse:  # pragma: no cover - dev hint
            return PlainTextResponse(
                "Manual not built. Run:\n"
                "  uv run --group docs sphinx-build -b html docs docs/_build/html\n",
                status_code=404,
            )

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
    ui.add_head_html(
        f'<link rel="stylesheet" href="{static_url("theme.css")}">', shared=True
    )
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestLogMiddleware)
    register_pages()


def run() -> None:
    init_logging()
    create_app()
    s = settings()
    secret = s.storage_secret
    if not secret or secret in (
        "dev-secret-change-me",
        "change-me-to-a-long-random-string",
    ):
        secret = secrets.token_hex(32)  # never serve forgeable sessions
        logger.warning(
            "VDB_STORAGE_SECRET unset — using an ephemeral secret; sessions reset on restart"
        )
    # Registered here, not in create_app(): tests boot create_app() and must
    # never start real job loops against the test database.
    if s.scheduler_enabled and not s.reload:
        app.on_startup(scheduler.start)
        app.on_shutdown(scheduler.stop)
    elif s.scheduler_enabled:
        logger.info(
            "scheduler disabled under VDB_RELOAD — reload restarts would re-fire it",
            audit=True,  # visible at the default AUDIT verbosity
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
        # uvicorn must not dictConfig its own handlers; its loggers then
        # propagate to root and render through structlog. The middleware's
        # http.request line replaces the access log (it adds user/ip/ms).
        uvicorn_logging_level="info",
        log_config=None,
        access_log=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
