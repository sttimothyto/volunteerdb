"""Authorization: global admin flag + per-team fourfold roles.

Rules (team roles cascade down to sub-teams):
- admin                 — everything, including team/user management and imports
- leader / second of T  — manage roster of T and its sub-teams; edit contact
                          info of volunteers on those teams
- core of T             — view full roster (incl. contact details) of T + sub-teams
- member of T           — view roster names of T (no contact details)
- any signed-in user    — browse the team directory, see/edit own profile
"""

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AppUser, Membership, TeamRole
from .services import teams as team_service


@dataclass(frozen=True)
class Actor:
    user: AppUser
    volunteer_id: int | None
    roles_by_team: dict[int, TeamRole]  # direct memberships
    managed_team_ids: set[int]  # leader/second teams incl. sub-teams
    full_view_team_ids: set[int]  # + core teams incl. sub-teams
    names_view_team_ids: set[int]  # + member teams (direct only)

    @property
    def is_admin(self) -> bool:
        return self.user.is_admin

    def can_manage_team(self, team_id: int) -> bool:
        return self.is_admin or team_id in self.managed_team_ids

    def can_view_full_roster(self, team_id: int) -> bool:
        return self.is_admin or team_id in self.full_view_team_ids

    def can_view_roster_names(self, team_id: int) -> bool:
        return self.can_view_full_roster(team_id) or team_id in self.names_view_team_ids

    def can_edit_volunteer(self, volunteer_id: int, volunteer_team_ids: set[int]) -> bool:
        """Contact-info edits: admin, self, or leader/second of one of their teams."""
        if self.is_admin or volunteer_id == self.volunteer_id:
            return True
        return bool(self.managed_team_ids & volunteer_team_ids)

    def can_view_volunteer(self, volunteer_id: int, volunteer_team_ids: set[int]) -> bool:
        """Full profile view: admin, self, or full-roster rights on a shared team."""
        if self.is_admin or volunteer_id == self.volunteer_id:
            return True
        return bool(self.full_view_team_ids & volunteer_team_ids)

    def can_view_capacity(self, volunteer_team_ids: set[int]) -> bool:
        """Capacity band/score: admins, or leaders/seconds of one of the
        volunteer's teams. Deliberately excludes core members AND the
        volunteer themself — capacity is a leadership planning signal."""
        return self.is_admin or bool(self.managed_team_ids & volunteer_team_ids)


async def load_actor(session: AsyncSession, user: AppUser) -> Actor:
    roles_by_team: dict[int, TeamRole] = {}
    if user.volunteer_id is not None:
        rows = await session.execute(
            sa.select(Membership.team_id, Membership.role).where(
                Membership.volunteer_id == user.volunteer_id
            )
        )
        roles_by_team = dict(rows.all())

    managed: set[int] = set()
    full_view: set[int] = set()
    names_view: set[int] = set()
    if roles_by_team:
        all_teams = await team_service.list_all(session)
        for team_id, role in roles_by_team.items():
            subtree = team_service.descendant_ids(all_teams, team_id)
            if role in (TeamRole.leader, TeamRole.second):
                managed |= subtree
            elif role == TeamRole.core:
                full_view |= subtree
            else:
                names_view.add(team_id)
    full_view |= managed

    return Actor(
        user=user,
        volunteer_id=user.volunteer_id,
        roles_by_team=roles_by_team,
        managed_team_ids=managed,
        full_view_team_ids=full_view,
        names_view_team_ids=names_view,
    )


async def volunteer_team_ids(session: AsyncSession, volunteer_id: int) -> set[int]:
    rows = await session.execute(
        sa.select(Membership.team_id).where(Membership.volunteer_id == volunteer_id)
    )
    return {team_id for (team_id,) in rows}


class Forbidden(PermissionError):
    """Raised by services/API when the actor lacks the required permission."""


def require(condition: bool, what: str = "this action") -> None:
    if not condition:
        raise Forbidden(f"not allowed: {what}")
