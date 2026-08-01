from decimal import Decimal

from fastapi import APIRouter

from ..permissions import require, team_ids_map
from ..services import volunteers as volunteer_service
from ..services import workload as service
from .deps import AsOf, CtxDep
from .schemas import BandOut, WorkloadConfigIn, WorkloadConfigOut, WorkloadScoreOut

router = APIRouter(prefix="/workload", tags=["workload"])


def _config_out(config: service.WorkloadConfig) -> WorkloadConfigOut:
    return WorkloadConfigOut(
        multipliers={role: float(m) for role, m in config.multipliers.items()},
        bands=[
            BandOut(
                label=b.label, color=b.color, upper=None if b.upper is None else float(b.upper)
            )
            for b in config.bands
        ],
    )


@router.get("/config")
async def get_config(ctx: CtxDep) -> WorkloadConfigOut:
    """Multipliers and band colors/thresholds; admin-only like the scores themselves."""
    require(ctx.actor.is_admin, "view workload config")
    return _config_out(await service.get_config(ctx.session))


@router.put("/config")
async def put_config(ctx: CtxDep, data: WorkloadConfigIn) -> WorkloadConfigOut:
    require(ctx.actor.is_admin, "only admins configure workload")
    config = service.WorkloadConfig(
        multipliers={role: Decimal(str(m)) for role, m in data.multipliers.items()},
        bands=[
            service.Band(b.label, b.color, None if b.upper is None else Decimal(str(b.upper)))
            for b in data.bands
        ],
    )
    await service.set_config(ctx.session, config)
    return _config_out(config)


@router.get("/scores")
async def workload_scores(ctx: CtxDep, as_of: AsOf) -> list[WorkloadScoreOut]:
    """Workload scores, restricted to volunteers whose workload the caller may see."""
    found = await volunteer_service.search(ctx.session, at=as_of, include_inactive=True)
    team_sets = await team_ids_map(ctx.session, [v.id for v in found], as_of)
    visible = await service.visible_scores(
        ctx.session, ctx.actor, {v.id: team_sets.get(v.id, set()) for v in found}, at=as_of
    )
    return [
        WorkloadScoreOut(volunteer_id=vid, score=float(score), band=band.label, color=band.color)
        for vid, (score, band) in sorted(visible.items())
    ]
