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
from ..errors import DomainError, Invalid, invalid, not_found, require
from ..fp import Err, Ok, Result
from ..models import CustomFieldDef, FieldType, Volunteer
from ..permissions import Actor, volunteer_team_ids
from . import volunteers as volunteer_service

_UNSET: object = object()


def _slugify(label: str) -> Result[str, Invalid]:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not slug:
        return invalid("label must contain letters or digits")
    return Ok(slug[:50])


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
    actor: Actor | None,
    label: str,
    field_type: FieldType | str,
    options: list | None = None,
    show_in_list: bool = False,
    position: int = 0,
) -> Result[CustomFieldDef, DomainError]:
    if denied := require(actor is None or actor.is_admin, "manage custom fields"):
        return denied
    label = (label or "").strip()
    if not label:
        return invalid("label is required")
    try:
        ft = FieldType(field_type)
    except ValueError:
        return invalid(f"unknown field type: {field_type!r}")
    opts = _clean_options(options) if ft is FieldType.select else None
    if ft is FieldType.select and not opts:
        return invalid("a choice field needs at least one option")
    key = _slugify(label)
    if isinstance(key, Err):
        return key
    defn = CustomFieldDef(
        key=key.value,
        label=label,
        field_type=ft.value,
        options=opts,
        show_in_list=show_in_list,
        position=position,
    )
    session.add(defn)
    await session.flush()
    return Ok(defn)


async def update_def(
    session: AsyncSession,
    actor: Actor | None,
    field_id: int,
    *,
    label: str | None = None,
    options: list | None | object = _UNSET,
    show_in_list: bool | None = None,
    position: int | None = None,
    is_active: bool | None = None,
) -> Result[CustomFieldDef, DomainError]:
    """key and field_type are immutable — stored values are keyed/typed by them."""
    if denied := require(actor is None or actor.is_admin, "manage custom fields"):
        return denied
    defn = await get_def(session, field_id)
    if defn is None:
        return not_found("custom field", field_id)
    if label is not None:
        label = label.strip()
        if not label:
            return invalid("label is required")
        defn.label = label
    if options is not _UNSET:
        if defn.field_type != FieldType.select.value:
            return invalid("only choice fields have options")
        opts = _clean_options(options)  # type: ignore[arg-type]
        if not opts:
            return invalid("a choice field needs at least one option")
        defn.options = opts
    if show_in_list is not None:
        defn.show_in_list = show_in_list
    if position is not None:
        defn.position = position
    if is_active is not None:
        defn.is_active = is_active
    await session.flush()
    return Ok(defn)


async def delete_def(
    session: AsyncSession, actor: Actor | None, field_id: int
) -> Result[None, DomainError]:
    """Hard delete; orphaned keys in Volunteer.custom simply stop being rendered."""
    if denied := require(actor is None or actor.is_admin, "manage custom fields"):
        return denied
    defn = await get_def(session, field_id)
    if defn is None:
        return not_found("custom field", field_id)
    await session.delete(defn)
    await session.flush()
    return Ok(None)


def validate_value(defn: CustomFieldDef, value: Any) -> Result[Any, Invalid]:
    """Normalize a raw value for storage, or the Invalid. None clears."""
    if value is None:
        return Ok(None)
    ft = FieldType(defn.field_type)
    if ft is FieldType.select:
        if value not in (defn.options or []):
            return invalid(
                f"{defn.label} must be one of: {', '.join(defn.options or [])}"
            )
        return Ok(value)
    parsed = fieldcodec.parse_scalar(ft, value)
    if isinstance(parsed, Err):
        return invalid(f"{defn.label} {parsed.error.message}")
    return parsed


async def set_values(
    session: AsyncSession,
    actor: Actor | None,
    volunteer_id: int,
    values: dict[str, Any],
) -> Result[Volunteer, DomainError]:
    """Validate and merge values into Volunteer.custom. A None value clears its key.

    Custom values are contact-tier data (they are redacted alongside phone and
    notes), so writing them asks for the same right as editing the volunteer —
    not the admin right that defining the *fields* takes."""
    volunteer = await volunteer_service.get(session, volunteer_id)
    if volunteer is None:
        return not_found("volunteer", volunteer_id)
    if actor is not None:
        if denied := require(
            actor.can_edit_volunteer(
                volunteer_id, await volunteer_team_ids(session, volunteer_id)
            ),
            "edit this volunteer",
        ):
            return denied
    defs = {d.key: d for d in await list_defs(session)}
    # always build a fresh dict: plain JSONB columns don't track in-place mutation
    merged = dict(volunteer.custom or {})
    for key, raw in values.items():
        defn = defs.get(key)
        if defn is None:
            return invalid(f"unknown custom field: {key}")
        checked = validate_value(defn, raw)
        if isinstance(checked, Err):
            return checked
        if checked.value is None:
            merged.pop(key, None)
        else:
            merged[key] = checked.value
    volunteer.custom = merged
    await session.flush()
    return Ok(volunteer)
