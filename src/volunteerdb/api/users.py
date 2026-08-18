from fastapi import APIRouter
from pydantic import BaseModel

from ..permissions import require
from ..services import users as service
from .deps import CtxDep
from .schemas import UserIn, UserOut, UserPatch

router = APIRouter(prefix="/users", tags=["users"])


def _user_out(user, invite_token: str | None = None) -> UserOut:
    """`invite_token` is passed in, never read off the row: only the digest is
    stored (services.users._issue_invite), so a freshly minted link is the only
    one that exists in readable form and the column itself must never be
    serialized. `invite_expires_at` still comes off the row — a caller may
    always learn that a link is outstanding, just not what it is."""
    out = UserOut.model_validate(user)
    out.has_password = user.password_hash is not None
    out.invite_token = invite_token
    return out


@router.get("")
async def list_users(ctx: CtxDep) -> list[UserOut]:
    require(ctx.actor.is_admin, "manage accounts")
    return [_user_out(u) for u in await service.list_all(ctx.session)]


@router.post("", status_code=201)
async def create_user(ctx: CtxDep, data: UserIn) -> UserOut:
    require(ctx.actor.is_admin, "manage accounts")
    user, token = await service.create(ctx.session, **data.model_dump())
    return _user_out(user, token)


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
    """Invalidate the password and issue a fresh invite link.

    Also revokes the account's API token: it was issued against the password
    being invalidated, and this route is how an admin acts on an account they
    believe is compromised."""
    require(ctx.actor.is_admin, "manage accounts")
    token = await service.reissue_invite(ctx.session, user_id)
    return _user_out(await service.get(ctx.session, user_id), token)


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
        created=[_user_out(u, token) for _, u, token in report.created],
        linked=[_user_out(u) for _, u in report.linked],
        skipped=[
            {"volunteer_id": v.id, "name": v.full_name, "reason": reason}
            for v, reason in report.skipped
        ],
    )
