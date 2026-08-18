from decimal import Decimal

from fastapi import APIRouter

from ..services import pages as page_service
from ..services import teams as service
from .deps import AsOf, CtxDep
from .schemas import (
    HomeDocPatch,
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
    fields = data.model_dump()
    _weight_to_decimal(fields)
    team = await service.create(ctx.session, ctx.actor, **fields)
    return TeamOut.model_validate(team)


@router.get("/{team_id}")
async def get_team(ctx: CtxDep, team_id: int, as_of: AsOf) -> TeamOut:
    team = await service.get(ctx.session, team_id, at=as_of)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    return TeamOut.model_validate(team)


@router.patch("/{team_id}")
async def update_team(ctx: CtxDep, team_id: int, data: TeamPatch) -> TeamOut:
    fields = data.model_dump(exclude_unset=True)
    if fields.pop("clear_parent", False):
        fields["parent_team_id"] = None
    if fields.pop("clear_workload_weight", False):
        fields["workload_weight"] = None
    else:
        _weight_to_decimal(fields)
    team = await service.update(ctx.session, ctx.actor, team_id, **fields)
    return TeamOut.model_validate(team)


@router.delete("/{team_id}", status_code=204)
async def delete_team(ctx: CtxDep, team_id: int) -> None:
    await service.delete(ctx.session, ctx.actor, team_id)


@router.patch("/{team_id}/home-doc")
async def set_home_doc(ctx: CtxDep, team_id: int, data: HomeDocPatch) -> TeamOut:
    """Set (or clear, with url=null) the public Google Doc behind the team's
    /ministries/ page. Unlike PATCH /teams/{id}, this is open to the team's
    leaders, seconds and core members.

    Core members are included **deliberately**, and it is not an oversight to
    be tightened: ministry leaders here are often elderly, and a public page
    nobody can refresh goes stale. Widening the group that may keep it current
    is worth more than narrowing who may speak for the ministry — the content
    is nh3-sanitized and the URL must live on docs.google.com, so the exposure
    is what the page says, under a name the parish can correct."""
    team = await page_service.set_home_doc_url(
        ctx.session, ctx.actor, team_id, data.url
    )
    return TeamOut.model_validate(team)


@router.get("/{team_id}/roster")
async def team_roster(ctx: CtxDep, team_id: int, as_of: AsOf) -> list[RosterEntry]:
    team = await service.get(ctx.session, team_id, at=as_of)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    rows = await service.roster(ctx.session, ctx.actor, team_id, at=as_of)
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
            )
        )
    return entries
