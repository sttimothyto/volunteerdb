"""Per-page/per-action helpers bridging NiceGUI sessions and the service layer."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps

from nicegui import app, context, ui
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import asof_param, effects, policy
from ..actors import load_actor
from ..db import transaction
from ..domain import Outcome
from ..effects import Effect
from ..env import Env, current
from ..errors import (
    Conflict,
    DomainError,
    DomainErrorRaised,
    Invalid,
    QueryError,
    Throttled,
    WeakPassword,
    message,
)
from ..errors import Forbidden as ForbiddenValue
from ..fp import Err, Ok, Result, as_result
from ..log import bind_actor
from ..permissions import Actor, Forbidden
from ..services import users as user_service
from . import column_order

SESSION_REMEMBER = timedelta(days=90)
SESSION_SHORT = timedelta(days=1)


def establish_session(
    user_id: int, *, remember: bool, method: str = "password"
) -> None:
    """Sign the browser in. `method` records which authenticator did it —
    "password", "otp" or "invite" — because /account needs to know: someone who
    got here with an emailed code has proved possession of the mailbox and may
    set a password without knowing the old one, which is what makes "I forgot
    it" self-serviceable (NIST SP 800-63B §4.1.2.1).

    The session id this lands in was already rotated on the way in — see
    main.AuthMiddleware, which does it where a Set-Cookie can still be sent."""
    app.storage.user["user_id"] = user_id
    app.storage.user["auth_method"] = method
    app.storage.user["session_expires_at"] = (
        datetime.now(UTC) + (SESSION_REMEMBER if remember else SESSION_SHORT)
    ).isoformat()


def clear_session() -> None:
    """Sign out. Keeps the dark-mode pref — that is how this browser likes to
    read, whoever is signed in — but drops the table column order, which is
    scoped to the sitting rather than to the machine."""
    app.storage.user.pop("user_id", None)
    app.storage.user.pop("auth_method", None)
    app.storage.user.pop("session_expires_at", None)
    app.storage.user.pop(column_order.STORAGE_KEY, None)


def session_auth_method() -> str:
    """How this session signed in. Unknown (a session predating the field)
    counts as "password" — the assumption that asks for more, not less."""
    return app.storage.user.get("auth_method") or "password"


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


async def get_actor(session: AsyncSession, *, env: Env | None = None) -> Actor | None:
    """The signed-in actor, or None. With an `env`, an admin's actor also
    carries the mail-allowance gauge (the page frame shows it; the asset
    routes, which pass none, never do)."""
    user_id = session_user_id()
    if user_id is None:
        return None
    user = await user_service.get(session, user_id)
    if user is None or not user.is_active:
        return None
    quota = None
    if env is not None and user.is_admin:
        quota = await env.quota.projection(env.sessions, env.today(), env.clock.now())
    return await load_actor(session, user, mail_quota=quota)


def _client_ip() -> str:
    try:
        return context.client.ip or "-"
    except Exception:  # background task or no client scope
        return "-"


def _base_url() -> str:
    """The origin the page was requested on, for links in mail and toasts. A
    handler over the websocket still sees the page's original request."""
    try:
        return str(context.client.request.base_url).rstrip("/")
    except Exception:  # background task or no client scope
        return ""


@dataclass(frozen=True)
class PageCtx:
    """One page load or one action: its transaction, who is asking, the Env
    the edge performs effects with, the moment it began (one clock read for
    every service that needs a `now`), the origin links are built on, and the
    as-of instant a time-travelling page was opened at."""

    session: AsyncSession
    actor: Actor
    env: Env
    now: datetime
    base_url: str
    as_of: datetime | None = None

    def policy_ctx(self) -> policy.PolicyCtx:
        """What the rules need, as values: the moment, the link base, this
        door's notify mode, a snapshot of the throttle ledger, the copy."""
        return policy.PolicyCtx(
            now=self.now,
            base_url=self.base_url,
            notify=self.env.notify,
            throttle=self.env.throttle.snapshot(),
            copy=self.env.mail_context(),
        )


@asynccontextmanager
async def page_ctx(as_of: datetime | None = None) -> AsyncIterator[PageCtx]:
    """For page builders and action handlers: the unit of work with the actor
    loaded, or a redirect to /login and a raise."""
    env = current()
    now = env.clock.now()
    # ExitStack outlives the transaction, so the actor identity is still bound
    # when the commit (and its audit marker line) fires.
    with ExitStack() as stack:
        async with transaction(env, session_user_id()) as session:
            actor = await get_actor(session, env=env)
            if actor is None:
                clear_session()
                ui.navigate.to("/login")
                raise Forbidden("not signed in")
            stack.enter_context(
                bind_actor(
                    f"{actor.user.id}:{actor.user.email}", ip=_client_ip(), via="gui"
                )
            )
            yield PageCtx(
                session=session,
                actor=actor,
                env=env,
                now=now,
                base_url=_base_url(),
                as_of=as_of,
            )


@asynccontextmanager
async def page_session() -> AsyncIterator[tuple[AsyncSession, Actor]]:
    """page_ctx() for the pages and handlers that still take (session, actor);
    each moves to page_ctx() as it becomes a load/render pair
    (FUNCTIONAL_REFACTORING.md, Phase 5)."""
    async with page_ctx() as ctx:
        yield ctx.session, ctx.actor


action_session = page_session  # same contract, used from event handlers


def split_outcome[T](value: Outcome[T] | T) -> tuple[T, tuple]:
    """A service's plain value, or its Outcome's value and events."""
    if isinstance(value, Outcome):
        return value.value, value.events
    return value, ()


async def run_command[T](
    command: Callable[[PageCtx], Awaitable[Result[Outcome[T] | T, DomainError]]],
    *,
    reload: bool = True,
    on_ok: Callable[[T, tuple[Effect, ...]], None] | None = None,
) -> Result[T, DomainError]:
    """One GUI action, start to finish.

    The command runs inside a page_ctx() unit of work and returns the
    service's Result. An Err rolls the transaction back and becomes a toast.
    An Ok is planned (policy.plan over its events) inside the transaction and
    committed; the effects -- mail, audit lines, throttle charges -- run AFTER
    the commit, so mail never rides a transaction; then `on_ok` (the success
    toast, a dialog to close: a pure function of the value and the effects)
    and, unless told otherwise, a reload. A conflict at commit (IntegrityError)
    is a Conflict toast. A command may still call .unwrap() on the way
    (transition); the carrier is read back as the Err it wraps.
    """
    env = current()
    try:
        async with page_ctx() as ctx:
            try:
                result = as_result(await command(ctx))
            except DomainErrorRaised as exc:
                result = Err(exc.error)
            if isinstance(result, Err):
                await ctx.session.rollback()
                toast(result.error)
                return result
            value, events = split_outcome(result.value)
            planned = policy.plan(events, ctx.policy_ctx())
    except IntegrityError:
        conflict = Conflict()
        toast(conflict)
        return Err(conflict)
    except Forbidden as exc:  # page_ctx: not signed in; it already redirected
        return Err(ForbiddenValue(str(exc)))
    await effects.run(planned, env)
    if on_ok is not None:
        on_ok(value, planned)
    if reload:
        ui.navigate.reload()
    return Ok(value)


def toast(err: DomainError) -> None:
    """The one place a refusal becomes a toast: a rule the reader can fix
    (bad input, a weak password, a query typo, a throttle) is a warning;
    anything else is a refusal in red."""
    soft = isinstance(err, (Invalid, WeakPassword, QueryError, Throttled))
    ui.notify(message(err), color="warning" if soft else "negative")


def notify_errors(handler: Callable) -> Callable:
    """Wrap an event handler: service-layer errors become toast notifications."""

    @wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except DomainErrorRaised as exc:  # transition: a converted service's Err
            toast(exc.error)
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
    """Query-param 'as of': a date means end of that day, local time. Shared with
    the API (see asof_param.py); a page ignores garbage and renders live data
    than erroring at the reader."""
    try:
        return asof_param.parse_as_of(raw)
    except ValueError:
        return None


def asof_banner(as_of: datetime, base_path: str) -> None:
    """The 'you are reading history' strip, carrying its own way back.

    Rendered by frame() in the page body: the picker hides in the header's
    settings menu, but a snapshot must never be silent."""
    with ui.row().classes("w-full bg-amber-100 rounded p-2 items-center gap-2"):
        ui.icon("history")
        ui.label(
            f"Read-only snapshot as of {as_of.astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
        ).classes("text-amber-900 font-medium")
        ui.space()
        ui.button("Back to now").props(f'dense color=warning href="{base_path}"')


def asof_picker(as_of: datetime | None, base_path: str) -> None:
    """Date picker for the header settings menu; clearing it returns to now."""
    from .date_input import date_input  # date_input imports a11y, which is ui-only

    field = date_input(
        "View as of (YYYY-MM-DD)",
        value=as_of.date().isoformat() if as_of is not None else "",
        clearable=True,
    ).classes("w-full")

    def go() -> None:
        value = (field.value or "").strip()
        ui.navigate.to(f"{base_path}?as_of={value}" if value else base_path)

    field.on("keydown.enter", go)
    ui.button("View", on_click=go).props("dense outline")
