from collections.abc import AsyncIterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import effects, policy
from ..actors import load_actor
from ..asof_param import parse_as_of
from ..domain import NotifyMode, Outcome
from ..env import Env
from ..errors import (
    BadCredentials,
    Conflict,
    DomainError,
    DomainErrorRaised,
    External,
    Invalid,
    NotFound,
    QueryError,
    Throttled,
    WeakPassword,
    message,
)
from ..errors import Forbidden as ForbiddenValue
from ..fp import Err, Result, as_result
from ..log import bind_actor
from ..permissions import Actor, Forbidden
from ..services import users as user_service

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Ctx:
    """One authenticated request: its transaction, who is asking, the Env the
    edge performs effects with, the moment the request began (one clock read,
    passed to every service that needs a `now`), and the origin links are
    built on."""

    session: AsyncSession
    actor: Actor
    env: Env
    now: datetime
    base_url: str
    # This door sends no roster mail of its own: a volunteer an API call
    # scheduled hears about it from the nightly digest (docs/reference/
    # http-api.md). The GUI's PageCtx runs the Env's mode, `direct`.
    notify: NotifyMode = NotifyMode.digest

    def policy_ctx(self) -> policy.PolicyCtx:
        """What the rules need, as values: the moment, the link base, this
        door's notify mode, a snapshot of the throttle ledger, the copy."""
        return policy.PolicyCtx(
            now=self.now,
            base_url=self.base_url,
            notify=self.notify,
            throttle=self.env.throttle.snapshot(),
            copy=self.env.mail_context(),
        )


def env_of(request: Request) -> Env:
    """The Env the app serving this request holds (main.create_app sets it;
    the test harness sets it on its bare router app)."""
    env = getattr(request.app.state, "env", None)
    if env is None:
        raise RuntimeError("no Env on this app: create_app() did not run")
    return env


async def api_ctx(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[Ctx]:
    """Authenticated request context: one transaction, actor loaded, and the
    user id recorded transaction-locally for the history triggers."""
    env = env_of(request)
    now = env.clock.now()
    base_url = str(request.base_url).rstrip("/")
    ip = request.client.host if request.client else "-"
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            401, "missing Bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    async with env.sessions() as session:
        # ExitStack outlives the transaction block, so the actor identity is
        # still bound when the commit (and its audit marker line) fires.
        with ExitStack() as stack:
            async with session.begin():
                user = await user_service.authenticate_token(session, token.strip())
                if user is None:
                    logger.warning("auth.api_token_invalid", ip=ip)
                    raise HTTPException(
                        401, "invalid token", headers={"WWW-Authenticate": "Bearer"}
                    )
                await session.execute(
                    sa.select(sa.func.set_config("app.user_id", str(user.id), True))
                )
                stack.enter_context(
                    bind_actor(f"{user.id}:{user.email}", ip=ip, via="api")
                )
                # admins only: nobody else can raise the plan or cut the sending
                quota = (
                    await env.quota.projection(env.sessions, env.today(), now)
                    if user.is_admin
                    else None
                )
                yield Ctx(
                    session=session,
                    actor=await load_actor(session, user, mail_quota=quota),
                    env=env,
                    now=now,
                    base_url=base_url,
                )


CtxDep = Annotated[Ctx, Depends(api_ctx)]


def as_of_param(
    as_of: Annotated[
        str | None,
        Query(
            description=(
                "view data as of this ISO date or timestamp; a bare date means the END "
                "of that day, so as_of=2026-07-30 includes everything that happened on "
                "the 30th. Naive timestamps are read in the server's local timezone."
            ),
            examples=["2026-07-30", "2026-07-30T14:00:00"],
        ),
    ] = None,
) -> datetime | None:
    """Parsed by the same helper the GUI uses, so a query string means the same
    thing on both surfaces. A malformed value raises ValueError, which the
    installed handler turns into a 422."""
    if as_of is None or not as_of.strip():
        return None
    return parse_as_of(as_of)


AsOf = Annotated[datetime | None, Depends(as_of_param)]


def status_of(err: DomainError) -> int:
    """The one place a refusal becomes an HTTP status."""
    match err:
        case ForbiddenValue():
            return 403
        case NotFound():
            return 404
        case Invalid() | WeakPassword() | QueryError():
            return 422
        case Conflict():
            return 409
        case Throttled():
            return 429
        case External():
            return 502
        case BadCredentials():
            return 401
    raise AssertionError(f"not a DomainError: {err!r}")  # pragma: no cover


def to_http(err: DomainError) -> HTTPException:
    headers = (
        {"WWW-Authenticate": "Bearer"} if isinstance(err, BadCredentials) else None
    )
    if isinstance(err, Throttled):
        headers = {"Retry-After": str(err.retry_after_s)}
    return HTTPException(status_of(err), message(err), headers=headers)


def raise_http[T](result: Result[T, DomainError] | T) -> T:
    """The value, or the refusal as the HTTPException it maps to (the
    transaction unwinds with it, so nothing partial commits)."""
    r = as_result(result)
    if isinstance(r, Err):
        raise to_http(r.error)
    return r.value


def dispatch[T](
    ctx: Ctx, background: BackgroundTasks, result: Result[Outcome[T] | T, DomainError]
) -> T:
    """A mutation's Result, seen through: the refusal becomes its status, the
    Outcome's events are planned (policy.plan) now and its effects run as
    background tasks -- after the response, which is after api_ctx committed
    -- so mail never rides a transaction. The plain value comes back for the
    response body."""
    value, events = _split(raise_http(result))
    planned = policy.plan(events, ctx.policy_ctx())
    if planned:
        background.add_task(effects.run, planned, ctx.env)
    return value


def _split[T](value: Outcome[T] | T) -> tuple[T, tuple]:
    if isinstance(value, Outcome):
        return value.value, value.events
    return value, ()


def install_exception_handlers(app: FastAPI) -> None:
    # Transition: a converted service called by an unconverted route raises
    # the carrier from Err.unwrap(); it maps exactly as the value would.
    @app.exception_handler(DomainErrorRaised)
    async def _domain_error(request: Request, exc: DomainErrorRaised):
        from fastapi.responses import JSONResponse

        http = to_http(exc.error)
        return JSONResponse(
            status_code=http.status_code,
            content={"detail": http.detail},
            headers=http.headers,
        )

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(LookupError)
    async def _not_found(request: Request, exc: LookupError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(IntegrityError)
    async def _conflict(request: Request, exc: IntegrityError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409, content={"detail": "conflicts with existing data"}
        )

    @app.exception_handler(ValueError)
    async def _unprocessable(request: Request, exc: ValueError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})
