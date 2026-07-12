from fastapi import APIRouter, HTTPException

from ..db import db_session
from ..services import users as service
from .deps import CtxDep
from .schemas import LoginIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(data: LoginIn) -> TokenOut:
    """Exchange email+password for the personal API token (issued on first login)."""
    async with db_session() as session:
        user = await service.authenticate(session, data.email, data.password)
        if user is None:
            raise HTTPException(401, "invalid credentials")
        token = user.api_token or await service.issue_api_token(session, user.id)
    return TokenOut(token=token)


@router.get("/me")
async def me(ctx: CtxDep) -> UserOut:
    out = UserOut.model_validate(ctx.actor.user)
    out.has_password = ctx.actor.user.password_hash is not None
    out.invite_token = None
    return out
