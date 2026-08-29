"""iCalendar feeds, and the one form that rotates a personal one.

Three GETs and a POST, none of them NiceGUI pages: a calendar client fetches
a feed with no cookie and no JavaScript, so these are plain Starlette
responses, registered from register_pages() the way ministries_routes are.

    /calendar/parish.ics          public — the same events the public Google
                                  calendar carries, straight from the database
    /calendar/mine/<token>.ics    the reader's own duties; the token in the
                                  path IS the credential (app_user.calendar_token)
    /calendar/mine.ics            the same, for the signed-in browser: an
                                  <a download> target, no token in the address
    POST /calendar/mine/reset     a new token; every client on the old address
                                  goes dark. A form, not a button handler, so
                                  the subscribe panel needs no websocket.
"""

from datetime import UTC, datetime

from fastapi import HTTPException, Request
from nicegui import app
from starlette.responses import RedirectResponse, Response

from ..actors import load_actor
from ..config import settings
from ..db import db_session
from ..env import current as current_env
from ..services import events as event_service
from ..services import gcal, ics
from ..services import users as user_service
from .context import get_actor

MEDIA_TYPE = "text/calendar; charset=utf-8"
# a client re-fetches about hourly; a quarter of that keeps a fresh edit
# from being served stale for long, at parish scale
PARISH_CACHE = {"Cache-Control": "public, max-age=900"}
PERSONAL_CACHE = {"Cache-Control": "private, max-age=900"}


def _origin(request: Request) -> tuple[str, str]:
    base_url = str(request.base_url).rstrip("/")
    return base_url, request.url.hostname or "volunteerdb"


def _window() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - ics.WINDOW_BACK, now + ics.WINDOW_FORWARD


def parish_feed_name() -> str:
    return gcal.calendar_name()


def personal_feed_name() -> str:
    org = settings().org_name.strip()
    return f"My duties — {org}" if org else "My duties"


async def parish_feed(request: Request) -> Response:
    base_url, host = _origin(request)
    from_, to = _window()
    async with db_session() as session:
        entries = (
            await event_service.calendar_entries(
                session, None, scope="parish", from_=from_, to=to
            )
        ).unwrap()
    body = ics.render(
        entries,
        name=parish_feed_name(),
        host=host,
        base_url=base_url,
        now=current_env().clock.now(),
        tz=current_env().tz,
    )
    return Response(content=body, media_type=MEDIA_TYPE, headers=PARISH_CACHE)


async def personal_feed(token: str, request: Request) -> Response:
    base_url, host = _origin(request)
    from_, to = _window()
    async with db_session() as session:
        user = await user_service.by_calendar_token(session, token)
        if user is None:
            raise HTTPException(404, "no such calendar")
        actor = await load_actor(session, user)
        entries = (
            await event_service.calendar_entries(
                session, actor, scope="mine", from_=from_, to=to
            )
        ).unwrap()
    body = ics.render(
        entries,
        name=personal_feed_name(),
        host=host,
        base_url=base_url,
        now=current_env().clock.now(),
        tz=current_env().tz,
    )
    return Response(content=body, media_type=MEDIA_TYPE, headers=PERSONAL_CACHE)


async def personal_download(request: Request) -> Response:
    base_url, host = _origin(request)
    from_, to = _window()
    async with db_session() as session:
        actor = await get_actor(session)
        if actor is None:
            raise HTTPException(401, "sign in to download your calendar")
        entries = (
            await event_service.calendar_entries(
                session, actor, scope="mine", from_=from_, to=to
            )
        ).unwrap()
    body = ics.render(
        entries,
        name=personal_feed_name(),
        host=host,
        base_url=base_url,
        now=current_env().clock.now(),
        tz=current_env().tz,
    )
    return Response(
        content=body,
        media_type=MEDIA_TYPE,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'attachment; filename="my-duties.ics"',
        },
    )


async def reset_personal(request: Request) -> Response:
    async with db_session() as session:
        actor = await get_actor(session)
        if actor is None:
            raise HTTPException(401, "sign in to reset your calendar address")
        (
            await user_service.reset_calendar_token(
                session, actor.user.id, token=current_env().rng.token()
            )
        ).unwrap()
    # back to the page the form was on — same origin only, or /events
    base_url, _ = _origin(request)
    referer = request.headers.get("referer", "")
    target = (
        referer[len(base_url) :] if referer.startswith(base_url + "/") else "/events"
    )
    return RedirectResponse(target or "/events", status_code=303)


def register() -> None:
    """Wire the routes onto the global app; called from register_pages() on
    every create_app() (see ministries_routes.register for why)."""
    app.get("/calendar/parish.ics", include_in_schema=False)(parish_feed)
    app.get("/calendar/mine/{token}.ics", include_in_schema=False)(personal_feed)
    app.get("/calendar/mine.ics", include_in_schema=False)(personal_download)
    app.post("/calendar/mine/reset", include_in_schema=False)(reset_personal)
