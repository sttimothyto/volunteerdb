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

from ..errors import DomainError, Invalid, invalid, require
from ..fp import Err, Ok, Result
from ..history import entity
from ..models import AppSetting, Membership, Team, TeamRole
from ..permissions import Actor

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
        Band("red", "#c62828", None),
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


# Admin-picked band colours are painted as badges, so each carries whichever
# label — ink or white — reads better on it, and a colour neither reads on
# is refused: a badge nobody can read is not a colour band.
INK = "#1c1917"
WHITE = "#ffffff"
MIN_LABEL_CONTRAST = 4.5  # WCAG 2.2 AA, 1.4.3


def _luminance(colour: str) -> float | None:
    """Relative luminance of a #rgb/#rrggbb colour; None for anything else."""
    hex6 = colour.strip().lstrip("#")
    if len(hex6) == 3:
        hex6 = "".join(ch * 2 for ch in hex6)
    if len(hex6) != 6:
        return None
    try:
        r, g, b = (int(hex6[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float | None:
    """WCAG contrast ratio, or None when either is not a colour."""
    la, lb = _luminance(a), _luminance(b)
    if la is None or lb is None:
        return None
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def text_colour(colour: str) -> str:
    """Ink or white, whichever stands out more against `colour` (ink when
    `colour` is not one at all — a stored band colour always is)."""
    return (
        INK if (contrast(INK, colour) or 0) >= (contrast(WHITE, colour) or 0) else WHITE
    )


def contrast_with_label(colour: str) -> float | None:
    """The ratio a badge's text reads at, or None for a non-colour."""
    return contrast(text_colour(colour), colour)


def validate_config(config: WorkloadConfig) -> Err[Invalid] | None:
    for band in config.bands:
        ratio = contrast_with_label(band.color)
        if ratio is None:
            return invalid(f"band {band.label!r}: not a colour: {band.color!r}")
        if ratio < MIN_LABEL_CONTRAST:
            return invalid(
                f"band {band.label!r}: no text reads on {band.color} "
                f"({ratio:.1f}:1; {MIN_LABEL_CONTRAST}:1 is the floor)"
            )
    if set(config.multipliers) != set(TeamRole):
        return invalid("multipliers must cover all four roles")
    if any(m < 0 for m in config.multipliers.values()):
        return invalid("multipliers must not be negative")
    if not config.bands:
        return invalid("at least one band is required")
    if config.bands[-1].upper is not None:
        return invalid("the last band must have no upper threshold")
    uppers = [b.upper for b in config.bands[:-1]]
    if any(u is None for u in uppers):
        return invalid("only the last band may be unbounded")
    if any(u <= 0 for u in uppers) or any(a >= b for a, b in zip(uppers, uppers[1:])):
        return invalid("band thresholds must be positive and ascending")
    if len({b.label for b in config.bands}) != len(config.bands):
        return invalid("band labels must be unique")
    return None


async def read_config(session: AsyncSession) -> WorkloadConfig:
    """The multipliers and bands, ungated: read on every page that renders a
    band — the legend, the volunteers table, the scores themselves — and those
    already gate on who may see a band at all (visible_scores)."""
    setting = await session.get(AppSetting, SETTING_KEY)
    return _from_json(setting.value) if setting else DEFAULT_CONFIG


async def get_config(
    session: AsyncSession, actor: Actor | None
) -> Result[WorkloadConfig, DomainError]:
    """The config as the *subject* of a request rather than a lookup behind
    one — GET /api/workload/config and the admin page — so the actor is
    checked; everything else calls read_config."""
    if denied := require(
        actor is None or actor.is_admin or bool(actor.managed_team_ids),
        "view the workload configuration",
    ):
        return denied
    return Ok(await read_config(session))


async def set_config(
    session: AsyncSession,
    actor: Actor | None,
    config: WorkloadConfig,
    *,
    now: datetime,
) -> Result[None, DomainError]:
    if denied := require(
        actor is None or actor.is_admin, "set the workload configuration"
    ):
        return denied
    if bad := validate_config(config):
        return bad
    stmt = pg_insert(AppSetting).values(key=SETTING_KEY, value=_to_json(config))
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value, "updated_at": now},
    )
    await session.execute(stmt)
    return Ok(None)


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
        config = await read_config(session)
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
    config = await read_config(session)
    raw = await scores(session, permitted, at=at, config=config)
    return {vid: (score, band_for(score, config)) for vid, score in raw.items()}
