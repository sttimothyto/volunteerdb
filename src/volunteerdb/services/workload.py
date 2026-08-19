"""Volunteer workload: score = Σ team.workload_weight × role multiplier.

Admin-configured role multipliers and color bands live in app_setting under
"workload" (missing key ⇒ DEFAULT_CONFIG); team weights on team.workload_weight
(NULL ⇒ counts 0). Scores are GLOBAL over all of a volunteer's memberships —
deliberately, so a leader can spot someone overloaded by *other* ministries.
Per-viewer visibility is gated by Actor.can_view_workload.

Config is not versioned: as-of scores use historical memberships and weights
but today's multipliers/thresholds.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity
from ..models import AppSetting, Membership, Team, TeamRole
from ..permissions import Actor, require

SETTING_KEY = "workload"


@dataclass(frozen=True)
class Band:
    label: str
    color: str
    upper: Decimal | None  # inclusive; None = unbounded (last band only)


@dataclass(frozen=True)
class WorkloadConfig:
    multipliers: dict[TeamRole, Decimal]
    bands: list[Band]  # ascending thresholds


DEFAULT_CONFIG = WorkloadConfig(
    multipliers={
        TeamRole.leader: Decimal("3"),
        TeamRole.second: Decimal("2"),
        TeamRole.core: Decimal("1.5"),
        TeamRole.member: Decimal("1"),
    },
    bands=[
        Band("green", "#4caf50", Decimal("4")),
        Band("amber", "#ffb300", Decimal("8")),
        Band("red", "#e53935", None),
    ],
)


def _to_json(config: WorkloadConfig) -> dict:
    return {
        "multipliers": {role.value: float(m) for role, m in config.multipliers.items()},
        "bands": [
            {
                "label": b.label,
                "color": b.color,
                "upper": None if b.upper is None else float(b.upper),
            }
            for b in config.bands
        ],
    }


def _from_json(value: dict) -> WorkloadConfig:
    # JSON numbers arrive as floats; go through str() to keep Decimals exact
    multipliers = {
        role: Decimal(
            str(value["multipliers"].get(role.value, DEFAULT_CONFIG.multipliers[role]))
        )
        for role in TeamRole
    }
    bands = [
        Band(
            str(b["label"]),
            str(b["color"]),
            None if b.get("upper") is None else Decimal(str(b["upper"])),
        )
        for b in value.get("bands", [])
    ]
    return WorkloadConfig(multipliers=multipliers, bands=bands or DEFAULT_CONFIG.bands)


def validate_config(config: WorkloadConfig) -> None:
    if set(config.multipliers) != set(TeamRole):
        raise ValueError("multipliers must cover all four roles")
    if any(m < 0 for m in config.multipliers.values()):
        raise ValueError("multipliers must not be negative")
    if not config.bands:
        raise ValueError("at least one band is required")
    if config.bands[-1].upper is not None:
        raise ValueError("the last band must have no upper threshold")
    uppers = [b.upper for b in config.bands[:-1]]
    if any(u is None for u in uppers):
        raise ValueError("only the last band may be unbounded")
    if any(u <= 0 for u in uppers) or any(a >= b for a, b in zip(uppers, uppers[1:])):
        raise ValueError("band thresholds must be positive and ascending")
    if len({b.label for b in config.bands}) != len(config.bands):
        raise ValueError("band labels must be unique")


async def get_config(
    session: AsyncSession, actor: Actor | None = None
) -> WorkloadConfig:
    """The multipliers and bands. `actor` is optional because the config is read
    on every page that renders a band — the legend, the volunteers table, the
    scores themselves — and those already gate on who may see a band at all
    (visible_scores). Pass an actor where the config is the *subject* of the
    request rather than a lookup behind one, i.e. GET /api/workload/config and
    the admin page."""
    require(
        actor is None or actor.is_admin or bool(actor.managed_team_ids),
        "view the workload configuration",
    )
    setting = await session.get(AppSetting, SETTING_KEY)
    return _from_json(setting.value) if setting else DEFAULT_CONFIG


async def set_config(
    session: AsyncSession, actor: Actor | None, config: WorkloadConfig
) -> None:
    require(actor is None or actor.is_admin, "set the workload configuration")
    validate_config(config)
    stmt = pg_insert(AppSetting).values(key=SETTING_KEY, value=_to_json(config))
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value, "updated_at": sa.func.now()},
    )
    await session.execute(stmt)


def band_for(score: Decimal, config: WorkloadConfig) -> Band:
    for band in config.bands:
        if band.upper is None or score <= band.upper:
            return band
    return config.bands[-1]


async def scores(
    session: AsyncSession,
    volunteer_ids: list[int] | None = None,
    at: datetime | None = None,
    config: WorkloadConfig | None = None,
) -> dict[int, Decimal]:
    """Global workload score per volunteer. With `volunteer_ids`, every requested
    id is present in the result (no memberships ⇒ 0). Scalar rows, so no fetch()
    is needed even for as-of queries."""
    if volunteer_ids is not None and not volunteer_ids:
        return {}
    if config is None:
        config = await get_config(session)
    M, T = entity(Membership, at), entity(Team, at)
    mult = sa.case(
        {
            role.value: sa.literal(config.multipliers[role], sa.Numeric(8, 2))
            for role in TeamRole
        },
        value=sa.cast(M.role, sa.String),
    )
    # coalesce the weight, not the sum: an as-of snapshot can join team_history
    # rows whose workload_weight predates the NOT NULL default and is still NULL
    # (NULL ⇒ counts 0, per the module docstring). Without this, a volunteer
    # whose memberships all land on NULL-weight rows sums to NULL and Decimal(None)
    # raises.
    stmt = (
        sa.select(
            M.volunteer_id,
            sa.func.sum(sa.func.coalesce(T.workload_weight, 0) * mult),
        )
        .join(T, T.id == M.team_id)
        .group_by(M.volunteer_id)
    )
    if volunteer_ids is not None:
        stmt = stmt.where(M.volunteer_id.in_(volunteer_ids))
    result = {vid: Decimal(total) for vid, total in (await session.execute(stmt)).all()}
    for vid in volunteer_ids or ():
        result.setdefault(vid, Decimal(0))
    return result


async def visible_scores(
    session: AsyncSession,
    actor: Actor,
    team_sets: dict[int, set[int]],
    at: datetime | None = None,
) -> dict[int, tuple[Decimal, Band]]:
    """(score, band) for exactly those volunteers whose workload `actor` may see.
    `team_sets` maps volunteer id -> ALL their team ids (drives the permission)."""
    permitted = [
        vid for vid, tids in team_sets.items() if actor.can_view_workload(tids)
    ]
    if not permitted:
        return {}
    config = await get_config(session)
    raw = await scores(session, permitted, at=at, config=config)
    return {vid: (score, band_for(score, config)) for vid, score in raw.items()}
