from fastapi import APIRouter

from ..permissions import team_ids_map
from ..services import volunteers as volunteer_service
from ..services import workload as service
from .deps import AsOf, CtxDep, raise_http
from .schemas import WorkloadConfigIn, WorkloadConfigOut, WorkloadScoreOut

router = APIRouter(prefix="/workload", tags=["workload"])


@router.get("/config")
async def get_config(ctx: CtxDep) -> WorkloadConfigOut:
    """Multipliers and band colors/thresholds; leaders need them to render workload."""
    return WorkloadConfigOut.of(
        raise_http(await service.get_config(ctx.session, ctx.actor))
    )


@router.put("/config")
async def put_config(ctx: CtxDep, data: WorkloadConfigIn) -> WorkloadConfigOut:
    config = data.to_config()
    raise_http(await service.set_config(ctx.session, ctx.actor, config, now=ctx.now))
    return WorkloadConfigOut.of(config)


@router.get("/scores")
async def workload_scores(ctx: CtxDep, as_of: AsOf) -> list[WorkloadScoreOut]:
    """Workload scores, restricted to volunteers whose workload the caller may see."""
    found = await volunteer_service.search(
        ctx.session, at=as_of, include_inactive=True, actor=ctx.actor
    )
    team_sets = await team_ids_map(ctx.session, [v.id for v in found], as_of)
    visible = await service.visible_scores(
        ctx.session,
        ctx.actor,
        {v.id: team_sets.get(v.id, set()) for v in found},
        at=as_of,
    )
    return [
        WorkloadScoreOut.of(vid, score, band)
        for vid, (score, band) in sorted(visible.items())
    ]
