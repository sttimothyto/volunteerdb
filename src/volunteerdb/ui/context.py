"""Per-page/per-action helpers bridging NiceGUI sessions and the service layer.

The edge kernel is ``api/deps``: the context a request carries, the
interpreter that runs a mutation's effects, the throttle pre-check. This
module is the GUI's face of it -- a ``PageCtx`` is a ``Ctx`` built inside a
page's unit of work, and the helpers below delegate to their API namesakes
with the process Env filled in, since a ``@ui.page`` function has no
dependency injection to hand one over.
"""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from nicegui import app, context, ui
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import asof_param, effects, policy
from ..actors import load_actor
from ..api import deps
from ..api.deps import Ctx, RequestFacts, split_outcome
from ..db import transaction
from ..domain import Outcome
from ..effects import Effect
from ..env import Env, current
from ..errors import (
    Conflict,
    DomainError,
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
        current().clock.now() + (SESSION_REMEMBER if remember else SESSION_SHORT)
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


def session_expired(raw: str | None, now: datetime) -> bool:
    """A stored expiry that has passed -- or is passing: like every other
    expiry here, a session is dead at the instant it names."""
    if not raw:
        return True
    try:
        return datetime.fromisoformat(raw) <= now
    except ValueError:
        return True


def session_user_id() -> int | None:
    """The signed-in user, or None once the session has run out.

    This is the primary expiry check: websocket-delivered actions from open
    tabs never pass through AuthMiddleware, but they all come through here."""
    user_id = app.storage.user.get("user_id")
    if user_id is None:
        return None
    if session_expired(
        app.storage.user.get("session_expires_at"), current().clock.now()
    ):
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
        return RequestFacts.from_request(context.client.request).base_url
    except Exception:  # background task or no client scope
        return ""


@dataclass(frozen=True)
class PageCtx(Ctx):
    """One page load or one action: the request context, built inside the
    page's unit of work with the Env's notify mode (`direct`: the people a
    write affects are mailed right after the commit) and the as-of instant a
    time-travelling page was opened at."""


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
            ip = _client_ip()
            stack.enter_context(
                bind_actor(f"{actor.user.id}:{actor.user.email}", ip=ip, via="gui")
            )
            yield PageCtx(
                session=session,
                actor=actor,
                env=env,
                now=now,
                base_url=_base_url(),
                ip=ip,
                notify=env.notify,
                as_of=as_of,
            )


async def run_command[T](
    command: Callable[[PageCtx], Awaitable[Result[Outcome[T] | T, DomainError]]],
    *,
    reload: bool = True,
    on_ok: Callable[[T, tuple[Effect, ...], effects.EffectReport], None] | None = None,
) -> Result[T, DomainError]:
    """One GUI action, start to finish.

    The command runs inside a page_ctx() unit of work and returns the
    service's Result. An Err rolls the transaction back and becomes a toast.
    An Ok is planned (policy.plan over its events) inside the transaction and
    committed; the effects -- mail, audit lines, throttle charges -- run AFTER
    the commit, so mail never rides a transaction; then `on_ok` (the success
    toast, a dialog to close: a function of the value, the effects planned
    and how they went) and, unless told otherwise, a reload. A conflict at commit (IntegrityError)
    is a Conflict toast.
    """
    env = current()
    try:
        async with page_ctx() as ctx:
            result = as_result(await command(ctx))
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
    report = await effects.run(planned, env)
    if on_ok is not None:
        outcome = on_ok(value, planned, report)
        if inspect.isawaitable(outcome):  # a tail that refreshes a widget
            await outcome
    if reload:
        ui.navigate.reload()
    return Ok(value)


def rate_limit(*keys: str, now: datetime, what: str) -> Err[Throttled] | None:
    """A front door's own pre-check as a value (api.deps.rate_limit, over
    the process Env): a throttled sign-in must not even reach authenticate().

        if denied := rate_limit(key, now=now, what="sign in"):
            toast(denied.error)
            return
    """
    return deps.rate_limit(current(), *keys, now=now, what=what)


async def perform(
    events, *, base_url: str, now: datetime | None = None
) -> effects.EffectReport:
    """The interpreter for a page with no signed-in actor (sign-in, an invite,
    a confirmation link) and for a fact the edge itself states (a failed
    attempt, a code requested): plan the events and run their effects now.
    The caller does this AFTER its transaction, as run_command does."""
    env = current()
    return await deps.perform(
        env,
        events,
        base_url=base_url,
        now=now if now is not None else env.clock.now(),
        notify=env.notify,
    )


def toast(err: DomainError) -> None:
    """The one place a refusal becomes a toast: a rule the reader can fix
    (bad input, a weak password, a query typo, a throttle) is a warning;
    anything else is a refusal in red."""
    soft = isinstance(err, (Invalid, WeakPassword, QueryError, Throttled))
    ui.notify(message(err), color="warning" if soft else "negative")


def parse_as_of(raw: str, tz: tzinfo) -> datetime | None:
    """Query-param 'as of': a date means end of that day, parish time (`tz` is
    the Env's). Shared with the API (see asof_param.py); a page ignores garbage
    and renders live data rather than erroring at the reader."""
    try:
        return asof_param.parse_as_of(raw, tz)
    except ValueError:
        return None


def asof_banner(as_of: datetime, base_path: str) -> None:
    """The 'you are reading history' strip, carrying its own way back.

    Rendered by frame() in the page body: the picker hides in the header's
    settings menu, but a snapshot must never be silent."""
    with ui.row().classes("w-full bg-amber-100 rounded p-2 items-center gap-2"):
        ui.icon("history")
        ui.label(
            f"Read-only snapshot as of {as_of.astimezone(current().tz).strftime('%Y-%m-%d %H:%M %Z')}"
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
