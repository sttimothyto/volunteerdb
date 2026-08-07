from fastapi import APIRouter
from pydantic import BaseModel

from ..models import TeamRole
from ..permissions import require
from ..services import memberships as service
from .deps import CtxDep
from .schemas import MembershipIn, MembershipOut

router = APIRouter(prefix="/memberships", tags=["memberships"])


class MembershipPatch(BaseModel):
    role: TeamRole | None = None


@router.post("", status_code=201)
async def assign(ctx: CtxDep, data: MembershipIn) -> MembershipOut:
    """Add a volunteer to a team, or change their role if already on it."""
    require(ctx.actor.can_manage_team(data.team_id), "manage this team's roster")
    membership = await service.assign(ctx.session, **data.model_dump())
    return MembershipOut.model_validate(membership)


@router.patch("/{membership_id}")
async def update(
    ctx: CtxDep, membership_id: int, data: MembershipPatch
) -> MembershipOut:
    membership = await service.get(ctx.session, membership_id)
    if membership is None:
        raise LookupError(f"membership {membership_id} not found")
    require(ctx.actor.can_manage_team(membership.team_id), "manage this team's roster")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(membership, field, value)
    await ctx.session.flush()
    return MembershipOut.model_validate(membership)


@router.delete("/{membership_id}", status_code=204)
async def remove(ctx: CtxDep, membership_id: int) -> None:
    membership = await service.get(ctx.session, membership_id)
    if membership is None:
        raise LookupError(f"membership {membership_id} not found")
    require(ctx.actor.can_manage_team(membership.team_id), "manage this team's roster")
    await service.remove(ctx.session, membership_id)
