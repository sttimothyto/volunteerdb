from fastapi import APIRouter, HTTPException, Request

from .. import throttle
from ..db import db_session
from ..services import users as service
from .deps import CtxDep
from .schemas import LoginIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(data: LoginIn, request: Request) -> TokenOut:
    """Exchange email+password for a fresh personal API token. Only a hash of
    the token is stored, so a new one is issued (revoking the old) on every
    login."""
    ip = request.client.host if request.client else "unknown"
    keys = (f"pw:{data.email.strip().lower()}", f"pw-ip:{ip}")
    if throttle.blocked(keys[0], 5, 900) or throttle.blocked(keys[1], 30, 900):
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    async with db_session() as session:
        user = await service.authenticate(session, data.email, data.password)
        if user is None:
            for key in keys:
                throttle.hit(key)
            raise HTTPException(401, "invalid credentials")
        token = await service.issue_api_token(session, user.id)
    return TokenOut(token=token)


@router.get("/me")
async def me(ctx: CtxDep) -> UserOut:
    out = UserOut.model_validate(ctx.actor.user)
    out.has_password = ctx.actor.user.password_hash is not None
    out.invite_token = None
    return out
