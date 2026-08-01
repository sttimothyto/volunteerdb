from decimal import Decimal

from fastapi import APIRouter

from ..permissions import require
from ..services import teams as service
from .deps import AsOf, CtxDep
from .schemas import (
    RosterEntry,
    TeamIn,
    TeamOut,
    TeamPatch,
    TeamWithPath,
    VolunteerOut,
    role_label,
)
from .volunteers import redacted

router = APIRouter(prefix="/teams", tags=["teams"])


def _weight_to_decimal(fields: dict) -> None:
    """asyncpg's numeric codec wants Decimal, not the float pydantic gives us."""
    if fields.get("workload_weight") is not None:
        fields["workload_weight"] = Decimal(str(fields["workload_weight"]))


@router.get("")
async def list_teams(ctx: CtxDep, as_of: AsOf) -> list[TeamWithPath]:
    """The team directory (structure is visible to every signed-in user)."""
    all_teams = await service.list_all(ctx.session, at=as_of)
    paths = service.team_paths(all_teams)
    return [
        TeamWithPath(**TeamOut.model_validate(t).model_dump(), path=paths[t.id])
        for t in all_teams
    ]


@router.post("", status_code=201)
async def create_team(ctx: CtxDep, data: TeamIn) -> TeamOut:
    require(ctx.actor.is_admin, "only admins create teams")
    fields = data.model_dump()
    _weight_to_decimal(fields)
    team = await service.create(ctx.session, **fields)
    return TeamOut.model_validate(team)


@router.get("/{team_id}")
async def get_team(ctx: CtxDep, team_id: int, as_of: AsOf) -> TeamOut:
    team = await service.get(ctx.session, team_id, at=as_of)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    return TeamOut.model_validate(team)


@router.patch("/{team_id}")
async def update_team(ctx: CtxDep, team_id: int, data: TeamPatch) -> TeamOut:
    require(ctx.actor.is_admin, "only admins edit teams")
    fields = data.model_dump(exclude_unset=True)
    if fields.pop("clear_parent", False):
        fields["parent_team_id"] = None
    if fields.pop("clear_workload_weight", False):
        fields["workload_weight"] = None
    else:
        _weight_to_decimal(fields)
    team = await service.update(ctx.session, team_id, **fields)
    return TeamOut.model_validate(team)


@router.delete("/{team_id}", status_code=204)
async def delete_team(ctx: CtxDep, team_id: int) -> None:
    require(ctx.actor.is_admin, "only admins delete teams")
    await service.delete(ctx.session, team_id)


@router.get("/{team_id}/roster")
async def team_roster(ctx: CtxDep, team_id: int, as_of: AsOf) -> list[RosterEntry]:
    require(ctx.actor.can_view_roster_names(team_id), "view this team's roster")
    team = await service.get(ctx.session, team_id, at=as_of)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    rows = await service.roster(ctx.session, team_id, at=as_of)
    full = ctx.actor.can_view_full_roster(team_id)
    manage = ctx.actor.can_manage_team(team_id)
    entries = []
    for membership, volunteer in rows:
        if full:
            out = VolunteerOut.model_validate(volunteer)
            if not manage:
                out.notes = None
        else:
            out = redacted(ctx.actor, volunteer, {team_id})
        entries.append(
            RosterEntry(
                membership_id=membership.id,
                volunteer=out,
                role=membership.role,
                role_label=role_label(membership.role),
                joined_on=membership.joined_on,
            )
        )
    return entries
