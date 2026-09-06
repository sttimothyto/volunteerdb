from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from ..domain import InviteIssued, Outcome
from ..fp import Ok
from ..services import users as service
from .deps import CtxDep, dispatch, raise_http
from .schemas import UserIn, UserOut, UserPatch

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
async def list_users(ctx: CtxDep) -> list[UserOut]:
    return [
        UserOut.of(u)
        for u in raise_http(await service.list_all(ctx.session, ctx.actor))
    ]


@router.post("", status_code=201)
async def create_user(
    ctx: CtxDep, data: UserIn, background: BackgroundTasks
) -> UserOut:
    """Admin-only, and the response carries the link, so nothing is mailed;
    the invite minted is still logged."""
    user, token = raise_http(
        await service.create(
            ctx.session, actor=ctx.actor, **data.model_dump(), invite=ctx.env.invite()
        )
    )
    if token:
        issued = InviteIssued(
            user.id, user.email, token, ctx.env.settings.invite_ttl_hours
        )
        dispatch(ctx, background, Ok(Outcome(None, (issued,))), silent=True)
    return UserOut.of(user, token)


@router.patch("/{user_id}")
async def update_user(ctx: CtxDep, user_id: int, data: UserPatch) -> UserOut:
    fields = data.model_dump(exclude_unset=True)
    # volunteer_id is a link, not a flag, and null means unlink — hence
    # exclude_unset above and a separate call rather than a None-means-skip arg
    link = fields.pop("volunteer_id", ...)
    user = raise_http(
        await service.set_flags(ctx.session, user_id, actor=ctx.actor, **fields)
    )
    if link is not ...:
        user = raise_http(
            await service.set_volunteer(ctx.session, user_id, link, actor=ctx.actor)
        )
    return UserOut.of(user)


@router.post("/{user_id}/reinvite")
async def reinvite(ctx: CtxDep, user_id: int, background: BackgroundTasks) -> UserOut:
    """Invalidate the password and issue a fresh invite link.

    Also revokes the account's API token: it was issued against the password
    being invalidated, and this route is how an admin acts on an account they
    believe is compromised."""
    token = dispatch(
        ctx,
        background,
        await service.reissue_invite(
            ctx.session, user_id, actor=ctx.actor, invite=ctx.env.invite()
        ),
        silent=True,  # the caller is an admin and gets the link here
    )
    return UserOut.of(await service.get(ctx.session, user_id), token)


class ProvisionOut(BaseModel):
    created: list[UserOut]
    linked: list[UserOut]
    skipped: list[dict]

    @classmethod
    def of(cls, report: service.ProvisionReport) -> "ProvisionOut":
        """The links come back in the body -- the one readable copy of each --
        so an admin can hand them out; nothing was mailed."""
        return cls(
            created=[UserOut.of(u, token) for _, u, token in report.created],
            linked=[UserOut.of(u) for _, u in report.linked],
            skipped=[
                {"volunteer_id": v.id, "name": v.full_name, "reason": reason}
                for v, reason in report.skipped
            ],
        )


@router.post("/provision")
async def provision(ctx: CtxDep, background: BackgroundTasks) -> ProvisionOut:
    """Create invite-token accounts for all active volunteers with an email
    and no account yet, and link existing unlinked accounts to the volunteer
    at the same address. The links come back in the body; nothing is mailed."""
    report = dispatch(
        ctx,
        background,
        await service.bulk_provision(ctx.session, ctx.actor, mint=ctx.env.invite),
        silent=True,
    )
    return ProvisionOut.of(report)
