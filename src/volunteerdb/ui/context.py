"""Per-page/per-action helpers bridging NiceGUI sessions and the service layer."""

from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from functools import wraps

from nicegui import app, context, ui
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import db_session
from ..log import bind_actor
from ..permissions import Actor, Forbidden, load_actor
from ..services import users as user_service

SESSION_REMEMBER = timedelta(days=90)
SESSION_SHORT = timedelta(days=1)


def establish_session(user_id: int, *, remember: bool) -> None:
    app.storage.user["user_id"] = user_id
    app.storage.user["session_expires_at"] = (
        datetime.now(UTC) + (SESSION_REMEMBER if remember else SESSION_SHORT)
    ).isoformat()


def clear_session() -> None:
    """Sign out; keeps e.g. the dark-mode pref."""
    app.storage.user.pop("user_id", None)
    app.storage.user.pop("session_expires_at", None)


def session_expired(raw: str | None) -> bool:
    if not raw:
        return True
    try:
        return datetime.fromisoformat(raw) < datetime.now(UTC)
    except ValueError:
        return True


def session_user_id() -> int | None:
    """The signed-in user, or None once the session has run out.

    This is the primary expiry check: websocket-delivered actions from open
    tabs never pass through AuthMiddleware, but they all come through here."""
    user_id = app.storage.user.get("user_id")
    if user_id is None:
        return None
    if session_expired(app.storage.user.get("session_expires_at")):
        clear_session()
        return None
    return user_id


async def get_actor(session: AsyncSession) -> Actor | None:
    user_id = session_user_id()
    if user_id is None:
        return None
    user = await user_service.get(session, user_id)
    if user is None or not user.is_active:
        return None
    return await load_actor(session, user)


def _client_ip() -> str:
    try:
        return context.client.ip or "-"
    except Exception:  # background task or no client scope
        return "-"


@asynccontextmanager
async def page_session() -> AsyncIterator[tuple[AsyncSession, Actor]]:
    """For page builders: session + actor, or redirects to /login and raises."""
    # ExitStack outlives db_session, so the actor identity is still bound when
    # the commit (and its audit marker line) fires.
    with ExitStack() as stack:
        async with db_session(session_user_id()) as session:
            actor = await get_actor(session)
            if actor is None:
                clear_session()
                ui.navigate.to("/login")
                raise Forbidden("not signed in")
            stack.enter_context(
                bind_actor(f"{actor.user.id}:{actor.user.email}", ip=_client_ip(), via="gui")
            )
            yield session, actor


action_session = page_session  # same contract, used from event handlers


def notify_errors(handler: Callable) -> Callable:
    """Wrap an event handler: service-layer errors become toast notifications."""

    @wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except Forbidden as exc:
            ui.notify(str(exc), color="negative")
        except LookupError as exc:
            ui.notify(str(exc), color="negative")
        except IntegrityError:
            ui.notify("conflicts with existing data (duplicate?)", color="negative")
        except ValueError as exc:
            ui.notify(str(exc), color="warning")

    return wrapper


def parse_as_of(raw: str) -> datetime | None:
    """Query-param 'as of': a date means end of that day, local time."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.time() == time.min and "T" not in raw:
        parsed += timedelta(days=1) - timedelta(microseconds=1)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def asof_banner(as_of: datetime | None, base_path: str) -> None:
    """Date picker + banner for read-only historical views."""
    with ui.row().classes("items-center gap-2"):
        date_input = ui.input("View as of (YYYY-MM-DD)").props("dense outlined clearable").classes("w-48")
        if as_of is not None:
            date_input.value = as_of.date().isoformat()

        def go() -> None:
            value = (date_input.value or "").strip()
            ui.navigate.to(f"{base_path}?as_of={value}" if value else base_path)

        ui.button("View", on_click=go).props("dense outline")
        if as_of is not None:
            ui.button("Back to now", on_click=lambda: ui.navigate.to(base_path)).props(
                "dense color=warning"
            )
    if as_of is not None:
        with ui.row().classes("w-full bg-amber-100 rounded p-2 items-center gap-2"):
            ui.icon("history")
            ui.label(
                f"Read-only snapshot as of {as_of.astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
            ).classes("text-amber-900 font-medium")
