"""Who is asking: the one loader that turns an account into an ``Actor``.

Split from ``permissions.py`` so that module stays a pure leaf -- the
``Actor`` value and the ``require`` gate every service imports -- while this
one may import the services it needs (the team tree, to expand a role over
its subtree) without closing the cycle that used to be broken with imports
inside the function body.

Called at the two front doors (``api/deps.py``, ``ui/context.py``) and by the
two orchestrators that act for a user without a request (the importer, the
roster sync). Everything an actor carries for the page frame -- their own
name, their headshot timestamp, the mail-allowance gauge for admins -- is
loaded here once, so ``frame()`` needs no session of its own.
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AppUser,
    Membership,
    ProposalVoter,
    TeamRole,
    Volunteer,
    VolunteerPhoto,
)
from .permissions import Actor
from .services import teams as team_service
from .services.mail_quota import Projection


async def load_actor(
    session: AsyncSession, user: AppUser, *, mail_quota: Projection | None = None
) -> Actor:
    """The actor for `user`. `mail_quota` is the allowance gauge the edge
    read for an admin (and only for an admin -- nobody else can act on it);
    it rides on the actor only while it is worth saying."""
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
        tree = await team_service.tree(session)
        for team_id, role in roles_by_team.items():
            subtree = tree.descendants(team_id)
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
    # meta roster, no edit rights over somebody else's members. team_service
    # owns the one definition of "this is a borrowed task-force roster"; the
    # exporter and the roster sync exclude the same teams through it.
    scope = managed | full_view
    meta_ids: set[int] = set()
    if scope:
        meta_ids = await team_service.meta_team_ids(session, scope)
    people = managed - meta_ids
    names_view |= full_view & meta_ids
    full_view -= meta_ids

    quota = None
    if user.is_admin and mail_quota is not None and mail_quota.alarming:
        quota = mail_quota

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
        mail_quota=quota,
    )
