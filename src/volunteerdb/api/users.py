from fastapi import APIRouter
from pydantic import BaseModel

from ..permissions import require
from ..services import users as service
from .deps import CtxDep
from .schemas import UserIn, UserOut, UserPatch

router = APIRouter(prefix="/users", tags=["users"])


def _user_out(user) -> UserOut:
    out = UserOut.model_validate(user)
    out.has_password = user.password_hash is not None
    return out


@router.get("")
async def list_users(ctx: CtxDep) -> list[UserOut]:
    require(ctx.actor.is_admin, "manage accounts")
    return [_user_out(u) for u in await service.list_all(ctx.session)]


@router.post("", status_code=201)
async def create_user(ctx: CtxDep, data: UserIn) -> UserOut:
    require(ctx.actor.is_admin, "manage accounts")
    user = await service.create(ctx.session, **data.model_dump())
    return _user_out(user)


@router.patch("/{user_id}")
async def update_user(ctx: CtxDep, user_id: int, data: UserPatch) -> UserOut:
    require(ctx.actor.is_admin, "manage accounts")
    user = await service.set_flags(
        ctx.session, user_id, **data.model_dump(exclude_unset=True)
    )
    return _user_out(user)


@router.post("/{user_id}/reinvite")
async def reinvite(ctx: CtxDep, user_id: int) -> UserOut:
    """Invalidate the password and issue a fresh invite link."""
    require(ctx.actor.is_admin, "manage accounts")
    await service.reissue_invite(ctx.session, user_id)
    return _user_out(await service.get(ctx.session, user_id))


class ProvisionOut(BaseModel):
    created: list[UserOut]
    skipped: list[dict]


@router.post("/provision")
async def provision(ctx: CtxDep) -> ProvisionOut:
    """Create invite-token accounts for all active volunteers with an email
    and no account yet."""
    require(ctx.actor.is_admin, "manage accounts")
    report = await service.bulk_provision(ctx.session)
    return ProvisionOut(
        created=[_user_out(u) for _, u in report.created],
        skipped=[
            {"volunteer_id": v.id, "name": v.full_name, "reason": reason}
            for v, reason in report.skipped
        ],
    )
