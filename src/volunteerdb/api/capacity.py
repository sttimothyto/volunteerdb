from decimal import Decimal

from fastapi import APIRouter

from ..permissions import require
from ..services import capacity as service
from ..services import volunteers as volunteer_service
from .deps import AsOf, CtxDep
from .schemas import BandOut, CapacityConfigIn, CapacityConfigOut, CapacityScoreOut
from .volunteers import _team_ids_map

router = APIRouter(prefix="/capacity", tags=["capacity"])


def _config_out(config: service.CapacityConfig) -> CapacityConfigOut:
    return CapacityConfigOut(
        multipliers={role: float(m) for role, m in config.multipliers.items()},
        bands=[
            BandOut(
                label=b.label, color=b.color, upper=None if b.upper is None else float(b.upper)
            )
            for b in config.bands
        ],
    )


@router.get("/config")
async def get_config(ctx: CtxDep) -> CapacityConfigOut:
    """Multipliers and band colors/thresholds; leaders need them to render capacity."""
    require(ctx.actor.is_admin or bool(ctx.actor.managed_team_ids), "view capacity config")
    return _config_out(await service.get_config(ctx.session))


@router.put("/config")
async def put_config(ctx: CtxDep, data: CapacityConfigIn) -> CapacityConfigOut:
    require(ctx.actor.is_admin, "only admins configure capacity")
    config = service.CapacityConfig(
        multipliers={role: Decimal(str(m)) for role, m in data.multipliers.items()},
        bands=[
            service.Band(b.label, b.color, None if b.upper is None else Decimal(str(b.upper)))
            for b in data.bands
        ],
    )
    await service.set_config(ctx.session, config)
    return _config_out(config)


@router.get("/scores")
async def capacity_scores(ctx: CtxDep, as_of: AsOf) -> list[CapacityScoreOut]:
    """Workload scores, restricted to volunteers whose capacity the caller may see."""
    found = await volunteer_service.search(ctx.session, at=as_of, include_inactive=True)
    team_sets = await _team_ids_map(ctx.session, [v.id for v in found], as_of)
    visible = await service.visible_scores(
        ctx.session, ctx.actor, {v.id: team_sets.get(v.id, set()) for v in found}, at=as_of
    )
    return [
        CapacityScoreOut(volunteer_id=vid, score=float(score), band=band.label, color=band.color)
        for vid, (score, band) in sorted(visible.items())
    ]
