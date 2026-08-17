"""Admin-defined custom volunteer fields.

Definitions live in custom_field_def; values ride Volunteer.custom (JSONB) and
are therefore system-versioned with the volunteer row. Definitions are NOT
versioned: historical snapshots render against the current definitions.
"""

import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .. import fieldcodec
from ..models import CustomFieldDef, FieldType, Volunteer
from . import volunteers as volunteer_service

_UNSET: object = object()


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not slug:
        raise ValueError("label must contain letters or digits")
    return slug[:50]


def _clean_options(options: list | None) -> list[str]:
    cleaned: list[str] = []
    for opt in options or []:
        text = str(opt).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


async def list_defs(
    session: AsyncSession, include_inactive: bool = False
) -> list[CustomFieldDef]:
    stmt = sa.select(CustomFieldDef).order_by(
        CustomFieldDef.position, CustomFieldDef.label
    )
    if not include_inactive:
        stmt = stmt.where(CustomFieldDef.is_active)
    return list((await session.execute(stmt)).scalars())


async def get_def(session: AsyncSession, field_id: int) -> CustomFieldDef | None:
    return await session.get(CustomFieldDef, field_id)


async def create_def(
    session: AsyncSession,
    label: str,
    field_type: FieldType | str,
    options: list | None = None,
    show_in_list: bool = False,
    position: int = 0,
) -> CustomFieldDef:
    label = (label or "").strip()
    if not label:
        raise ValueError("label is required")
    ft = FieldType(field_type)  # ValueError on unknown type
    opts = _clean_options(options) if ft is FieldType.select else None
    if ft is FieldType.select and not opts:
        raise ValueError("a choice field needs at least one option")
    defn = CustomFieldDef(
        key=_slugify(label),
        label=label,
        field_type=ft.value,
        options=opts,
        show_in_list=show_in_list,
        position=position,
    )
    session.add(defn)
    await session.flush()
    return defn


async def update_def(
    session: AsyncSession,
    field_id: int,
    *,
    label: str | None = None,
    options: list | None | object = _UNSET,
    show_in_list: bool | None = None,
    position: int | None = None,
    is_active: bool | None = None,
) -> CustomFieldDef:
    """key and field_type are immutable — stored values are keyed/typed by them."""
    defn = await get_def(session, field_id)
    if defn is None:
        raise LookupError(f"custom field {field_id} not found")
    if label is not None:
        label = label.strip()
        if not label:
            raise ValueError("label is required")
        defn.label = label
    if options is not _UNSET:
        if defn.field_type != FieldType.select.value:
            raise ValueError("only choice fields have options")
        opts = _clean_options(options)  # type: ignore[arg-type]
        if not opts:
            raise ValueError("a choice field needs at least one option")
        defn.options = opts
    if show_in_list is not None:
        defn.show_in_list = show_in_list
    if position is not None:
        defn.position = position
    if is_active is not None:
        defn.is_active = is_active
    await session.flush()
    return defn


async def delete_def(session: AsyncSession, field_id: int) -> None:
    """Hard delete; orphaned keys in Volunteer.custom simply stop being rendered."""
    defn = await get_def(session, field_id)
    if defn is None:
        raise LookupError(f"custom field {field_id} not found")
    await session.delete(defn)
    await session.flush()


def validate_value(defn: CustomFieldDef, value: Any) -> Any:
    """Normalize a raw value for storage, or raise ValueError. None clears."""
    if value is None:
        return None
    ft = FieldType(defn.field_type)
    if ft is FieldType.select:
        if value not in (defn.options or []):
            raise ValueError(
                f"{defn.label} must be one of: {', '.join(defn.options or [])}"
            )
        return value
    try:
        return fieldcodec.parse_scalar(ft, value)
    except ValueError as exc:
        raise ValueError(f"{defn.label} {exc}") from None


async def set_values(
    session: AsyncSession, volunteer_id: int, values: dict[str, Any]
) -> Volunteer:
    """Validate and merge values into Volunteer.custom. A None value clears its key."""
    volunteer = await volunteer_service.get(session, volunteer_id)
    if volunteer is None:
        raise LookupError(f"volunteer {volunteer_id} not found")
    defs = {d.key: d for d in await list_defs(session)}
    # always build a fresh dict: plain JSONB columns don't track in-place mutation
    merged = dict(volunteer.custom or {})
    for key, raw in values.items():
        defn = defs.get(key)
        if defn is None:
            raise ValueError(f"unknown custom field: {key}")
        normalized = validate_value(defn, raw)
        if normalized is None:
            merged.pop(key, None)
        else:
            merged[key] = normalized
    volunteer.custom = merged
    await session.flush()
    return volunteer
