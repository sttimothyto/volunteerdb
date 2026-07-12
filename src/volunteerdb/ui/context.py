"""Per-page/per-action helpers bridging NiceGUI sessions and the service layer."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from functools import wraps

from nicegui import app, ui
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import db_session
from ..permissions import Actor, Forbidden, load_actor
from ..services import users as user_service


def session_user_id() -> int | None:
    return app.storage.user.get("user_id")


async def get_actor(session: AsyncSession) -> Actor | None:
    user_id = session_user_id()
    if user_id is None:
        return None
    user = await user_service.get(session, user_id)
    if user is None or not user.is_active:
        return None
    return await load_actor(session, user)


@asynccontextmanager
async def page_session() -> AsyncIterator[tuple[AsyncSession, Actor]]:
    """For page builders: session + actor, or redirects to /login and raises."""
    async with db_session(session_user_id()) as session:
        actor = await get_actor(session)
        if actor is None:
            app.storage.user.pop("user_id", None)  # keep e.g. the dark-mode pref
            ui.navigate.to("/login")
            raise Forbidden("not signed in")
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
