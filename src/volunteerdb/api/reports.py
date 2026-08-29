from fastapi import APIRouter

from ..models import TeamRole
from ..permissions import require
from ..services import graph as graph_service
from ..services import reports as service
from ..services import stats as stats_service
from .deps import AsOf, CtxDep
from .schemas import CoverageOut, DashboardStatsOut

router = APIRouter(tags=["reports"])


@router.get("/reports/coverage")
async def coverage(ctx: CtxDep, as_of: AsOf) -> list[CoverageOut]:
    """Teams and their role headcounts; missing leadership sorts first.
    Admins see the whole parish, leaders see the teams they manage."""
    require(
        ctx.actor.is_admin or bool(ctx.actor.managed_team_ids),
        "view the coverage report",
    )
    rows = await service.coverage(ctx.session, at=as_of)
    if not ctx.actor.is_admin:
        rows = [r for r in rows if r.team.id in ctx.actor.managed_team_ids]
    return [
        CoverageOut(
            team_id=r.team.id,
            path=r.path,
            leader=r.counts.get(TeamRole.leader, 0),
            second=r.counts.get(TeamRole.second, 0),
            core=r.counts.get(TeamRole.core, 0),
            member=r.counts.get(TeamRole.member, 0),
            total=r.total,
            missing_leader=r.missing_leader,
            missing_second=r.missing_second,
        )
        for r in rows
    ]


@router.get("/reports/dashboard")
async def dashboard(ctx: CtxDep, as_of: AsOf) -> DashboardStatsOut:
    """The dashboard's statistics, tiered: parish-wide figures for admins,
    what leadership must act on, and the caller's own service.

    No `require` here on purpose — there is no single right to ask for. Each
    tier carries its own predicate inside the service and comes back null for
    a caller without it, so this endpoint answers everyone and tells nobody
    anything they could not already reach by navigating.
    """
    figures = await stats_service.dashboard(
        ctx.session, ctx.actor, at=as_of, now=ctx.now, today=ctx.env.today()
    )
    return DashboardStatsOut.model_validate(figures)


@router.get("/graph")
async def graph(ctx: CtxDep, as_of: AsOf, team_id: int | None = None) -> dict:
    """Cytoscape.js elements for the volunteer↔team graph, filtered to what
    the caller may see."""
    return await graph_service.elements(
        ctx.session, ctx.actor, team_id=team_id, at=as_of
    )
