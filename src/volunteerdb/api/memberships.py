from fastapi import APIRouter
from pydantic import BaseModel

from ..models import TeamRole
from ..services import memberships as service
from .deps import CtxDep, raise_http
from .schemas import MembershipIn, MembershipOut

router = APIRouter(prefix="/memberships", tags=["memberships"])


class MembershipPatch(BaseModel):
    role: TeamRole | None = None


@router.post("", status_code=201)
async def assign(ctx: CtxDep, data: MembershipIn) -> MembershipOut:
    """Add a volunteer to a team, or change their role if already on it."""
    membership = raise_http(
        await service.assign(ctx.session, ctx.actor, **data.model_dump())
    )
    return MembershipOut.model_validate(membership)


@router.patch("/{membership_id}")
async def update(
    ctx: CtxDep, membership_id: int, data: MembershipPatch
) -> MembershipOut:
    """Change the role. `role` is the only field: `joined_on` and `notes` were
    dropped early, and a membership is otherwise identified by the pair it
    joins."""
    fields = data.model_dump(exclude_unset=True)
    # get_managed authorizes the read, so a body with nothing in it cannot be
    # used to report who holds what on a team the caller has no rights over
    membership = raise_http(
        await service.get_managed(ctx.session, ctx.actor, membership_id)
    )
    if fields.get("role") is not None:
        membership = raise_http(
            await service.set_role(
                ctx.session, ctx.actor, membership_id, fields["role"]
            )
        )
    return MembershipOut.model_validate(membership)


@router.delete("/{membership_id}", status_code=204)
async def remove(ctx: CtxDep, membership_id: int) -> None:
    raise_http(await service.remove(ctx.session, ctx.actor, membership_id))
