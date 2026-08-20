from decimal import Decimal

import httpx
from fastapi import APIRouter

from ..services import pages as page_service
from ..services import teams as service
from .deps import AsOf, CtxDep
from .schemas import (
    HomeDocPatch,
    RosterEntry,
    RosterSheetPatch,
    TeamIn,
    TeamOut,
    TeamPageOut,
    TeamPatch,
    TeamSheetOut,
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
    tree = await service.tree(ctx.session, at=as_of)
    return [
        TeamWithPath(**TeamOut.model_validate(t).model_dump(), path=tree.paths[t.id])
        for t in tree.teams
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
        fields["workload_weight"] = Decimal(0)  # 0 IS unweighted
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


@router.get("/{team_id}/roster-sheet")
async def get_roster_sheet(ctx: CtxDep, team_id: int) -> TeamSheetOut | None:
    """The team's Google Drive roster sheet: its link, the last sync's outcome,
    and any repoint an admin has asked for but no sync has checked yet.

    Null until the nightly job has made the team a sheet. Management rights,
    matching what the team page shows — the sheet is the roster, so who may see
    the link is who may manage the roster."""
    sheet = await service.roster_sheet(ctx.session, ctx.actor, team_id)
    return None if sheet is None else TeamSheetOut.model_validate(sheet)


@router.patch("/{team_id}/roster-sheet")
async def set_roster_sheet(
    ctx: CtxDep, team_id: int, data: RosterSheetPatch
) -> TeamSheetOut:
    """Point the team at a different roster spreadsheet, or send url=null to
    withdraw a request no sync has acted on yet.

    **Admin-only** — deliberately not the wider group that may set the team's
    home-page doc. Nothing here reaches Drive: the app cannot, so the link is
    recorded as a request and the next nightly sync is what verifies it, keeps
    the team on its current sheet if the file is invisible or is not a roster,
    and reports the outcome in `last_status`/`last_error`."""
    if data.url is None:
        sheet = await service.cancel_roster_sheet_request(
            ctx.session, ctx.actor, team_id
        )
    else:
        sheet = await service.request_roster_sheet(
            ctx.session, ctx.actor, team_id, data.url, import_rows=data.import_rows
        )
    return TeamSheetOut.model_validate(sheet)


@router.get("/{team_id}/roster")
async def team_roster(ctx: CtxDep, team_id: int, as_of: AsOf) -> list[RosterEntry]:
    # the fetch is only to 404 an id nobody has: an unknown team would
    # otherwise answer with an empty roster, which reads as "nobody serves here"
    if await service.get(ctx.session, team_id, at=as_of) is None:
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


@router.get("/{team_id}/page")
async def get_team_page(ctx: CtxDep, team_id: int) -> TeamPageOut | None:
    """Whether the team's public page is publishing, and when it last fetched.

    Null when the team has no home doc set. The page's HTML is not here: it is
    served to the world at /ministries/<slug>.html, and what a caller needs from
    JSON is whether the nightly fetch is working."""
    page = await page_service.page_status(ctx.session, ctx.actor, team_id)
    return None if page is None else TeamPageOut.model_validate(page)


@router.post("/{team_id}/page/fetch")
async def fetch_team_page(ctx: CtxDep, team_id: int) -> TeamPageOut:
    """Refetch the team's doc now, instead of waiting for the nightly job.

    Same rights as setting the URL — core members included, deliberately, so a
    ministry is not blocked on one person to keep its public page current. A
    failed fetch keeps the last good page and reports itself in `status`, rather
    than blanking what the world can see."""
    team = await service.get(ctx.session, team_id)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    if not team.home_doc_url:
        raise ValueError("this team has no home page doc")
    async with httpx.AsyncClient() as http:
        page = await page_service.fetch_and_store(
            ctx.session, team, http, force=True, actor=ctx.actor
        )
    return TeamPageOut.model_validate(page)
