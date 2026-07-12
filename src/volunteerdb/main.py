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
    app.add_middleware(AuthMiddleware)
    register_pages()


def run() -> None:
    create_app()
    s = settings()
    ui.run(
        host=s.host,
        port=s.port,
        title="St. Timothy VolunteerDB",
        favicon="⛪",
        storage_secret=s.storage_secret,
        reload=s.reload,
        show=False,
        fastapi_docs=True,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
