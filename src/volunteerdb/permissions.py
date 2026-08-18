"""Authorization: global admin flag + per-team fourfold roles.

Rules (team roles cascade down to sub-teams):
- admin                 — everything, including team/user management and
                          parish-wide spreadsheet import/export
- leader / second of T  — manage roster of T and its sub-teams; edit contact
                          info of volunteers on those teams; spreadsheet
                          import/export scoped to those teams
- core of T             — view full roster (incl. contact details) of T + sub-teams
- leader/second/core     — invite a volunteer on those teams to create an account
                          (an account-creation link only; account management
                          proper stays admin-only)
- member of T           — view roster names of T (no contact details)
- any signed-in user    — browse the team directory, see/edit own profile
"""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .history import entity
from .models import (
    AppUser,
    EventTaskForce,
    Membership,
    ProposalVoter,
    TeamRole,
    Volunteer,
    VolunteerPhoto,
)


@dataclass(frozen=True)
class Actor:
    user: AppUser
    volunteer_id: int | None
    # Two managing scopes, and the difference is the whole point. A task-force
    # team is a temporary roster copied from several real teams so they can
    # staff one event (services/task_force.py) -- run its AFFAIRS, yes; own its
    # PEOPLE, no. Otherwise adding any team as a collaborator would hand you
    # their members' contact details, notes, workload and invite links, which
    # is exactly the escalation this split closes.
    managed_team_ids: set[int]  # affairs: roster, events, elections (incl. task forces)
    people_team_ids: set[int]  # people: contact edits, workload, invites, exports
    full_view_team_ids: set[int]  # + core teams incl. sub-teams; never a task force
    names_view_team_ids: set[int]  # + member teams (direct) and task forces
    voter_proposal_ids: frozenset[int] = frozenset()  # rolls the actor sits on
    # Their own name and headshot timestamp, for the header avatar: the frame
    # renders before any page has a session to look them up with, and every
    # page already receives the actor.
    volunteer_name: str | None = None
    photo_at: datetime | None = None

    @property
    def is_admin(self) -> bool:
        return self.user.is_admin

    def can_manage_team(self, team_id: int) -> bool:
        return self.is_admin or team_id in self.managed_team_ids

    @property
    def can_access_elections(self) -> bool:
        """Elections page and nav: admins, managers, and anyone on a roll
        (voters keep access after a proposal is decided, to see the result)."""
        return (
            self.is_admin
            or bool(self.managed_team_ids)
            or bool(self.voter_proposal_ids)
        )

    def can_view_proposal(self, proposal_id: int, team_id: int) -> bool:
        return self.can_manage_team(team_id) or proposal_id in self.voter_proposal_ids

    @property
    def can_create_events(self) -> bool:
        """The events page's "New event" button: admins, plus anyone who
        leads (or seconds) a team. Per-event management is can_manage_team
        on the event's team."""
        return self.is_admin or bool(self.managed_team_ids)

    @property
    def can_import_export(self) -> bool:
        """Import/Export page and POST /api/import: admins, plus anyone who
        leads (or seconds) a team. Non-admin imports are scoped row-by-row
        to managed teams inside the importer."""
        return self.is_admin or bool(self.managed_team_ids)

    def can_view_full_roster(self, team_id: int) -> bool:
        return self.is_admin or team_id in self.full_view_team_ids

    def can_view_roster_names(self, team_id: int) -> bool:
        return self.can_view_full_roster(team_id) or team_id in self.names_view_team_ids

    def can_edit_volunteer(
        self, volunteer_id: int, volunteer_team_ids: set[int]
    ) -> bool:
        """Contact-info edits: admin, self, or leader/second of one of their teams.

        people_team_ids, not managed_team_ids: leading a task force is not
        leading the people it borrowed."""
        if self.is_admin or volunteer_id == self.volunteer_id:
            return True
        return bool(self.people_team_ids & volunteer_team_ids)

    def can_view_volunteer(
        self, volunteer_id: int, volunteer_team_ids: set[int]
    ) -> bool:
        """Full profile view: admin, self, or full-roster rights on a shared team."""
        if self.is_admin or volunteer_id == self.volunteer_id:
            return True
        return bool(self.full_view_team_ids & volunteer_team_ids)

    def can_invite_volunteer(self, volunteer_team_ids: set[int]) -> bool:
        """Send an account-creation link to a volunteer on one of their teams:
        admin, or leader/second/core of a team the volunteer is on.

        Wider than can_edit_volunteer — core members are included, because they
        already read the full roster, contact details and all, and they are the
        people who notice a missing account. Narrower than account management
        proper, which stays admin-only at /admin/users: this mints one
        non-admin account linked to that one volunteer and nothing else.

        No self clause (unlike can_edit_volunteer): the actor is signed in, so
        they already have the account this would create."""
        return self.is_admin or bool(self.full_view_team_ids & volunteer_team_ids)

    def can_view_workload(self, volunteer_team_ids: set[int]) -> bool:
        """Workload band/score: admins, or leaders/seconds of one of the
        volunteer's teams. Deliberately excludes core members AND the
        volunteer themself — workload is a leadership planning signal.
        people_team_ids, so a task force never reveals a borrowed member's."""
        return self.is_admin or bool(self.people_team_ids & volunteer_team_ids)


async def load_actor(session: AsyncSession, user: AppUser) -> Actor:
    roles_by_team: dict[int, TeamRole] = {}
    voter_proposal_ids: frozenset[int] = frozenset()
    volunteer_name: str | None = None
    photo_at: datetime | None = None
    if user.volunteer_id is not None:
        rows = await session.execute(
            sa.select(Membership.team_id, Membership.role).where(
                Membership.volunteer_id == user.volunteer_id
            )
        )
        roles_by_team = dict(rows.all())
        rolls = await session.execute(
            sa.select(ProposalVoter.proposal_id).where(
                ProposalVoter.volunteer_id == user.volunteer_id
            )
        )
        voter_proposal_ids = frozenset(rolls.scalars())
        # one indexed lookup, and never the image bytes: the header wants a
        # name for the dialog title and a timestamp for the ?v= cache-buster
        me = (
            await session.execute(
                sa.select(
                    Volunteer.first_name,
                    Volunteer.last_name,
                    VolunteerPhoto.uploaded_at,
                )
                .outerjoin(VolunteerPhoto, VolunteerPhoto.volunteer_id == Volunteer.id)
                .where(Volunteer.id == user.volunteer_id)
            )
        ).first()
        if me is not None:
            volunteer_name = f"{me.first_name} {me.last_name}"
            photo_at = me.uploaded_at

    managed: set[int] = set()
    full_view: set[int] = set()
    names_view: set[int] = set()
    if roles_by_team:
        # Imported here, not at module scope, and the cycle is the real reason:
        # authorization needs the team tree to expand a role into a subtree,
        # while every service — services.teams included — needs `Actor` and
        # `require` to enforce its own rules. That mutual need is the point of
        # the design (one check, in the one place both front doors pass
        # through), so it is resolved at the single point of use.
        from .services import teams as team_service

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

    # Task-force teams are borrowed rosters, not ministries anyone owns (see
    # the Actor field comments). They stay in `managed` so their event is still
    # managed and in `names_view` so you can see who is staffing it, but they
    # are cut out of `people` and `full_view` — no contact details through the
    # meta roster, no edit rights over somebody else's members. One tiny query:
    # the table holds a row per collaborative event and is torn down after.
    meta_ids: set[int] = set()
    scope = managed | full_view
    if scope:
        meta_ids = set(
            await session.scalars(
                sa.select(EventTaskForce.team_id).where(
                    EventTaskForce.team_id.in_(scope)
                )
            )
        )
    people = managed - meta_ids
    names_view |= full_view & meta_ids
    full_view -= meta_ids

    return Actor(
        user=user,
        volunteer_id=user.volunteer_id,
        managed_team_ids=managed,
        people_team_ids=people,
        full_view_team_ids=full_view,
        names_view_team_ids=names_view,
        voter_proposal_ids=voter_proposal_ids,
        volunteer_name=volunteer_name,
        photo_at=photo_at,
    )


async def team_ids_map(
    session: AsyncSession, volunteer_ids: list[int], at: datetime | None = None
) -> dict[int, set[int]]:
    """volunteer id -> their team ids, in one query for any number of volunteers.
    Every requested id is present in the result (no memberships ⇒ empty set)."""
    if not volunteer_ids:
        return {}
    M = entity(Membership, at)
    rows = await session.execute(
        sa.select(M.volunteer_id, M.team_id).where(M.volunteer_id.in_(volunteer_ids))
    )
    result: dict[int, set[int]] = {vid: set() for vid in volunteer_ids}
    for v_id, t_id in rows:
        result[v_id].add(t_id)
    return result


async def volunteer_team_ids(session: AsyncSession, volunteer_id: int) -> set[int]:
    return (await team_ids_map(session, [volunteer_id]))[volunteer_id]


class Forbidden(PermissionError):
    """Raised by services/API when the actor lacks the required permission."""


def require(condition: bool, what: str = "this action") -> None:
    if not condition:
        raise Forbidden(f"not allowed: {what}")
