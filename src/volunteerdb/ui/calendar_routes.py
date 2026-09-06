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

Each handler reads the Env, the request's facts and the moment once, at the
door (`_at_the_door`), and the moment is the Env's clock -- so the window a
feed covers moves with a test's clock the way every other page does.
"""

from datetime import datetime

from fastapi import HTTPException, Request
from nicegui import app
from starlette.responses import RedirectResponse, Response

from ..actors import load_actor
from ..api.deps import RequestFacts, raise_http
from ..db import transaction
from ..env import Env
from ..env import current as current_env
from ..services import events as event_service
from ..services import gcal, ics
from ..services import users as user_service
from ..services.events import CalendarEntry
from .context import get_actor

MEDIA_TYPE = "text/calendar; charset=utf-8"
# a client re-fetches about hourly; a quarter of that keeps a fresh edit
# from being served stale for long, at parish scale
PARISH_CACHE = {"Cache-Control": "public, max-age=900"}
PERSONAL_CACHE = {"Cache-Control": "private, max-age=900"}
DOWNLOAD_HEADERS = {
    "Cache-Control": "private, no-store",
    "Content-Disposition": 'attachment; filename="my-duties.ics"',
}


def _at_the_door(request: Request) -> tuple[Env, RequestFacts, datetime]:
    """The Env, what the request states about itself, and the moment -- read
    once per request, the way a page's PageCtx reads them."""
    env = current_env()
    return env, RequestFacts.from_request(request), env.clock.now()


def _window(now: datetime) -> tuple[datetime, datetime]:
    """The span a feed covers, around the Env's `now`."""
    return now - ics.WINDOW_BACK, now + ics.WINDOW_FORWARD


def parish_feed_name(env: Env) -> str:
    return gcal.calendar_name(env.settings.org_name)


def personal_feed_name(env: Env) -> str:
    org = env.settings.org_name.strip()
    return f"My duties — {org}" if org else "My duties"


def _feed(
    entries: list[CalendarEntry],
    *,
    env: Env,
    facts: RequestFacts,
    now: datetime,
    name: str,
    headers: dict[str, str],
) -> Response:
    """The rendered feed as a response: the one call to ics.render."""
    body = ics.render(
        entries,
        name=name,
        host=facts.host,
        base_url=facts.base_url,
        now=now,
        tz=env.tz,
    )
    return Response(content=body, media_type=MEDIA_TYPE, headers=headers)


async def parish_feed(request: Request) -> Response:
    env, facts, now = _at_the_door(request)
    from_, to = _window(now)
    async with transaction(env, None) as session:
        entries = raise_http(
            await event_service.calendar_entries(
                session, None, scope="parish", from_=from_, to=to
            )
        )
    return _feed(
        entries,
        env=env,
        facts=facts,
        now=now,
        name=parish_feed_name(env),
        headers=PARISH_CACHE,
    )


async def personal_feed(token: str, request: Request) -> Response:
    env, facts, now = _at_the_door(request)
    from_, to = _window(now)
    async with transaction(env, None) as session:
        user = await user_service.by_calendar_token(session, token)
        if user is None:
            raise HTTPException(404, "no such calendar")
        actor = await load_actor(session, user)
        entries = raise_http(
            await event_service.calendar_entries(
                session, actor, scope="mine", from_=from_, to=to
            )
        )
    return _feed(
        entries,
        env=env,
        facts=facts,
        now=now,
        name=personal_feed_name(env),
        headers=PERSONAL_CACHE,
    )


async def personal_download(request: Request) -> Response:
    env, facts, now = _at_the_door(request)
    from_, to = _window(now)
    async with transaction(env, None) as session:
        actor = await get_actor(session)
        if actor is None:
            raise HTTPException(401, "sign in to download your calendar")
        entries = raise_http(
            await event_service.calendar_entries(
                session, actor, scope="mine", from_=from_, to=to
            )
        )
    return _feed(
        entries,
        env=env,
        facts=facts,
        now=now,
        name=personal_feed_name(env),
        headers=DOWNLOAD_HEADERS,
    )


async def reset_personal(request: Request) -> Response:
    env, facts, _now = _at_the_door(request)
    async with transaction(env, None) as session:
        actor = await get_actor(session)
        if actor is None:
            raise HTTPException(401, "sign in to reset your calendar address")
        raise_http(
            await user_service.reset_calendar_token(
                session, actor.user.id, token=env.rng.token()
            )
        )
    # back to the page the form was on — same origin only, or /events
    referer = request.headers.get("referer", "")
    target = (
        referer[len(facts.base_url) :]
        if referer.startswith(facts.base_url + "/")
        else "/events"
    )
    return RedirectResponse(target or "/events", status_code=303)


def register() -> None:
    """Wire the routes onto the global app; called from register_pages() on
    every create_app() (see ministries_routes.register for why)."""
    app.get("/calendar/parish.ics", include_in_schema=False)(parish_feed)
    app.get("/calendar/mine/{token}.ics", include_in_schema=False)(personal_feed)
    app.get("/calendar/mine.ics", include_in_schema=False)(personal_download)
    app.post("/calendar/mine/reset", include_in_schema=False)(reset_personal)
