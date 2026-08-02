from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity
from ..models import Membership, Team, TeamRole
from . import teams as team_service


@dataclass
class CoverageRow:
    team: Team
    path: str
    counts: dict[TeamRole, int]
    total: int

    @property
    def missing_leader(self) -> bool:
        return self.counts.get(TeamRole.leader, 0) == 0

    @property
    def missing_second(self) -> bool:
        return self.counts.get(TeamRole.second, 0) == 0


async def coverage(
    session: AsyncSession,
    at: datetime | None = None,
    teams: list[Team] | None = None,
) -> list[CoverageRow]:
    """Role headcounts per team; the dashboard's 'holes to fill' report.

    `teams` lets a caller that already listed them (the teams page needs the
    inactive ones too, which never become rows here) hand them over instead of
    paying for a second list_all; it must be the same `at` snapshot.
    """
    all_teams = teams if teams is not None else await team_service.list_all(session, at)
    paths = team_service.team_paths(all_teams)
    M = entity(Membership, at)
    rows = (
        await session.execute(
            sa.select(M.team_id, M.role, sa.func.count()).group_by(M.team_id, M.role)
        )
    ).all()
    by_team: dict[int, dict[TeamRole, int]] = {}
    for team_id, role, count in rows:
        by_team.setdefault(team_id, {})[role] = count

    result = [
        CoverageRow(
            team=t,
            path=paths[t.id],
            counts=by_team.get(t.id, {}),
            total=sum(by_team.get(t.id, {}).values()),
        )
        for t in all_teams
        if t.is_active
    ]
    # teams with holes first, then by path for stable reading
    result.sort(
        key=lambda r: (not r.missing_leader, not r.missing_second, r.path.lower())
    )
    return result
