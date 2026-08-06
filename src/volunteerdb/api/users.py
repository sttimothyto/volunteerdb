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
    fields = data.model_dump(exclude_unset=True)
    # volunteer_id is a link, not a flag, and null means unlink — hence
    # exclude_unset above and a separate call rather than a None-means-skip arg
    link = fields.pop("volunteer_id", ...)
    user = await service.set_flags(ctx.session, user_id, **fields)
    if link is not ...:
        user = await service.set_volunteer(ctx.session, user_id, link)
    return _user_out(user)


@router.post("/{user_id}/reinvite")
async def reinvite(ctx: CtxDep, user_id: int) -> UserOut:
    """Invalidate the password and issue a fresh invite link."""
    require(ctx.actor.is_admin, "manage accounts")
    await service.reissue_invite(ctx.session, user_id)
    return _user_out(await service.get(ctx.session, user_id))


class ProvisionOut(BaseModel):
    created: list[UserOut]
    linked: list[UserOut]
    skipped: list[dict]


@router.post("/provision")
async def provision(ctx: CtxDep) -> ProvisionOut:
    """Create invite-token accounts for all active volunteers with an email
    and no account yet, and link existing unlinked accounts to the volunteer
    at the same address."""
    require(ctx.actor.is_admin, "manage accounts")
    report = await service.bulk_provision(ctx.session)
    return ProvisionOut(
        created=[_user_out(u) for _, u in report.created],
        linked=[_user_out(u) for _, u in report.linked],
        skipped=[
            {"volunteer_id": v.id, "name": v.full_name, "reason": reason}
            for v, reason in report.skipped
        ],
    )
