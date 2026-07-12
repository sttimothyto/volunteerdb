"""Cytoscape.js elements for the volunteer↔team graph.

Shared by the /api/graph endpoint and the NiceGUI graph page.
Output format: {"nodes": [{"data": {...}}], "edges": [{"data": {...}}]}
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..history import entity
from ..models import Membership, Team, TeamRole, Volunteer
from ..permissions import Actor
from . import teams as team_service


async def elements(
    session: AsyncSession,
    actor: Actor,
    team_id: int | None = None,
    at: datetime | None = None,
) -> dict:
    all_teams = await team_service.list_all(session, at)
    paths = team_service.team_paths(all_teams)

    visible_ids = {
        t.id for t in all_teams if actor.is_admin or actor.can_view_roster_names(t.id)
    }
    if team_id is not None:
        visible_ids &= team_service.descendant_ids(all_teams, team_id)
    shown_teams = [t for t in all_teams if t.id in visible_ids]

    M, V = entity(Membership, at), entity(Volunteer, at)
    rows = (
        await session.execute(
            sa.select(M.team_id, sa.cast(M.role, sa.String), V.id, V.first_name, V.last_name)
            .join(V, V.id == M.volunteer_id)
            .where(M.team_id.in_(visible_ids))
        )
    ).all() if visible_ids else []

    nodes = [
        {
            "data": {
                "id": f"t{t.id}",
                "label": t.name,
                "type": "team",
                "team_id": t.id,
                "path": paths[t.id],
            }
        }
        for t in shown_teams
    ]
    seen_volunteers: set[int] = set()
    edges = []
    for m_team_id, role, v_id, first, last in rows:
        if v_id not in seen_volunteers:
            seen_volunteers.add(v_id)
            nodes.append(
                {
                    "data": {
                        "id": f"v{v_id}",
                        "label": f"{first} {last}",
                        "type": "volunteer",
                        "volunteer_id": v_id,
                    }
                }
            )
        edges.append(
            {
                "data": {
                    "id": f"m{m_team_id}-{v_id}",
                    "source": f"v{v_id}",
                    "target": f"t{m_team_id}",
                    "role": role,
                    "leadership": role in (TeamRole.leader.value, TeamRole.second.value),
                }
            }
        )
    for t in shown_teams:
        if t.parent_team_id in visible_ids:
            edges.append(
                {
                    "data": {
                        "id": f"h{t.id}",
                        "source": f"t{t.id}",
                        "target": f"t{t.parent_team_id}",
                        "role": "subteam",
                        "hierarchy": True,
                    }
                }
            )
    return {"nodes": nodes, "edges": edges}
