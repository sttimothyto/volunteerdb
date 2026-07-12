from pathlib import Path

from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .api import api_router
from .api.deps import install_exception_handlers
from .config import settings
from .ui import register_pages

UNRESTRICTED_PREFIXES = ("/login", "/invite/", "/api/", "/_nicegui", "/static/", "/favicon")


class AuthMiddleware(BaseHTTPMiddleware):
    """Send anonymous browsers to /login. The JSON API under /api authenticates
    itself with Bearer tokens and is exempt here."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not any(path.startswith(p) for p in UNRESTRICTED_PREFIXES):
            if not app.storage.user.get("user_id"):
                return RedirectResponse(f"/login?redirect_to={path}")
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
    ui.run(
        host=s.host,
        port=s.port,
        title="VolunteerDB",
        favicon="⛪",
        storage_secret=s.storage_secret,
        reload=s.reload,
        show=False,
        fastapi_docs=True,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
