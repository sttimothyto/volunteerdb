import sqlalchemy as sa
from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity
from ..models import Membership, Volunteer
from ..permissions import Actor, require, volunteer_team_ids
from ..services import custom_fields as custom_field_service
from ..services import volunteers as service
from .deps import AsOf, CtxDep
from .schemas import (
    AssignmentOut,
    ImpactOut,
    VolunteerIn,
    VolunteerOut,
    VolunteerPatch,
    role_label,
)

router = APIRouter(prefix="/volunteers", tags=["volunteers"])


async def _team_ids_map(
    session: AsyncSession, volunteer_ids: list[int], at=None
) -> dict[int, set[int]]:
    if not volunteer_ids:
        return {}
    M = entity(Membership, at)
    rows = await session.execute(
        sa.select(M.volunteer_id, M.team_id).where(M.volunteer_id.in_(volunteer_ids))
    )
    result: dict[int, set[int]] = {}
    for v_id, t_id in rows:
        result.setdefault(v_id, set()).add(t_id)
    return result


def redacted(actor: Actor, volunteer: Volunteer, team_ids: set[int]) -> VolunteerOut:
    """Everyone may see names; contact details, notes and custom values need closer ties."""
    out = VolunteerOut.model_validate(volunteer)
    if not actor.can_view_volunteer(volunteer.id, team_ids):
        out.email = out.phone = out.notes = out.custom = None
    elif not actor.can_edit_volunteer(volunteer.id, team_ids):
        out.notes = None
    return out


@router.get("")
async def list_volunteers(
    ctx: CtxDep, as_of: AsOf, q: str = "", include_inactive: bool = False
) -> list[VolunteerOut]:
    found = await service.search(ctx.session, q, at=as_of, include_inactive=include_inactive)
    teams_map = await _team_ids_map(ctx.session, [v.id for v in found], as_of)
    return [redacted(ctx.actor, v, teams_map.get(v.id, set())) for v in found]


@router.post("", status_code=201)
async def create_volunteer(ctx: CtxDep, data: VolunteerIn) -> VolunteerOut:
    require(ctx.actor.is_admin, "only admins create volunteers")
    volunteer = await service.create(ctx.session, **data.model_dump())
    return VolunteerOut.model_validate(volunteer)


@router.get("/{volunteer_id}")
async def get_volunteer(ctx: CtxDep, volunteer_id: int, as_of: AsOf) -> VolunteerOut:
    volunteer = await service.get(ctx.session, volunteer_id, at=as_of)
    if volunteer is None:
        raise LookupError(f"volunteer {volunteer_id} not found")
    team_ids = (await _team_ids_map(ctx.session, [volunteer_id], as_of)).get(volunteer_id, set())
    return redacted(ctx.actor, volunteer, team_ids)


@router.patch("/{volunteer_id}")
async def update_volunteer(ctx: CtxDep, volunteer_id: int, data: VolunteerPatch) -> VolunteerOut:
    team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
    require(ctx.actor.can_edit_volunteer(volunteer_id, team_ids), "edit this volunteer")
    fields = data.model_dump(exclude_unset=True)
    if "is_active" in fields:
        require(ctx.actor.is_admin, "only admins archive volunteers")
    custom = fields.pop("custom", None)
    volunteer = await service.update(ctx.session, volunteer_id, **fields)
    if custom is not None:
        volunteer = await custom_field_service.set_values(ctx.session, volunteer_id, custom)
    return redacted(ctx.actor, volunteer, team_ids)


@router.delete("/{volunteer_id}", status_code=204)
async def delete_volunteer(ctx: CtxDep, volunteer_id: int) -> None:
    require(ctx.actor.is_admin, "only admins delete volunteers")
    await service.delete(ctx.session, volunteer_id)


@router.get("/{volunteer_id}/assignments")
async def volunteer_assignments(ctx: CtxDep, volunteer_id: int, as_of: AsOf) -> list[AssignmentOut]:
    """Which teams does this person serve on? Visible to all signed-in users."""
    rows = await service.assignments(ctx.session, volunteer_id, at=as_of)
    return [
        AssignmentOut(
            membership_id=m.id, team=t, role=m.role, role_label=role_label(m.role)
        )
        for m, t in rows
    ]


@router.get("/{volunteer_id}/impact")
async def volunteer_impact(ctx: CtxDep, volunteer_id: int, as_of: AsOf) -> list[ImpactOut]:
    """If this volunteer leaves, what holes appear?"""
    team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
    require(
        ctx.actor.can_view_volunteer(volunteer_id, team_ids), "view this volunteer's impact"
    )
    rows = await service.impact(ctx.session, volunteer_id, at=as_of)
    return [
        ImpactOut(
            team=r.team,
            role=r.role,
            role_label=role_label(r.role),
            leaders_left=r.leaders_left,
            leadership_left=r.leadership_left,
        )
        for r in rows
    ]
