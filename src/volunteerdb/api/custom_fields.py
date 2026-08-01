from fastapi import APIRouter

from ..permissions import require
from ..services import custom_fields as service
from .deps import CtxDep
from .schemas import CustomFieldDefIn, CustomFieldDefOut, CustomFieldDefPatch

router = APIRouter(prefix="/custom-fields", tags=["custom-fields"])


@router.get("")
async def list_custom_fields(
    ctx: CtxDep, include_inactive: bool = False
) -> list[CustomFieldDefOut]:
    """Field definitions. Readable by any signed-in caller — they're needed to
    interpret the `custom` dict on volunteers."""
    defs = await service.list_defs(ctx.session, include_inactive=include_inactive)
    return [CustomFieldDefOut.model_validate(d) for d in defs]


@router.post("", status_code=201)
async def create_custom_field(ctx: CtxDep, data: CustomFieldDefIn) -> CustomFieldDefOut:
    require(ctx.actor.is_admin, "only admins define custom fields")
    defn = await service.create_def(ctx.session, **data.model_dump())
    return CustomFieldDefOut.model_validate(defn)


@router.patch("/{field_id}")
async def update_custom_field(
    ctx: CtxDep, field_id: int, data: CustomFieldDefPatch
) -> CustomFieldDefOut:
    require(ctx.actor.is_admin, "only admins edit custom fields")
    defn = await service.update_def(
        ctx.session, field_id, **data.model_dump(exclude_unset=True)
    )
    return CustomFieldDefOut.model_validate(defn)


@router.delete("/{field_id}", status_code=204)
async def delete_custom_field(ctx: CtxDep, field_id: int) -> None:
    require(ctx.actor.is_admin, "only admins delete custom fields")
    await service.delete_def(ctx.session, field_id)
