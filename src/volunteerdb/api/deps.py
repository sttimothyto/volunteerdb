from collections.abc import AsyncIterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..asof_param import parse_as_of
from ..db import sessionmaker
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
from ..log import bind_actor
from ..permissions import Actor, Forbidden, load_actor
from ..services import users as user_service

logger = structlog.get_logger(__name__)


@dataclass
class Ctx:
    session: AsyncSession
    actor: Actor


async def api_ctx(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[Ctx]:
    """Authenticated request context: one transaction, actor loaded, and the
    user id recorded transaction-locally for the history triggers."""
    ip = request.client.host if request.client else "-"
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            401, "missing Bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    async with sessionmaker()() as session:
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
                yield Ctx(session=session, actor=await load_actor(session, user))


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
