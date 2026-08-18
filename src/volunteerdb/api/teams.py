from decimal import Decimal

import httpx
from fastapi import APIRouter

from ..services import interest as interest_service
from ..services import pages as page_service
from ..services import teams as service
from .deps import AsOf, CtxDep
from .schemas import (
    ApplicationFormPatch,
    HomeDocPatch,
    InterestOut,
    RosterEntry,
    TeamIn,
    TeamOut,
    TeamPageOut,
    TeamPatch,
    TeamWithPath,
    VolunteerOut,
    role_label,
)
from .volunteers import redacted

router = APIRouter(prefix="/teams", tags=["teams"])


def _weight_to_decimal(fields: dict) -> None:
    """asyncpg's numeric codec wants Decimal, not the float pydantic gives us."""
    if fields.get("workload_weight") is not None:
        fields["workload_weight"] = Decimal(str(fields["workload_weight"]))


@router.get("")
async def list_teams(ctx: CtxDep, as_of: AsOf) -> list[TeamWithPath]:
    """The team directory (structure is visible to every signed-in user)."""
    all_teams = await service.list_all(ctx.session, at=as_of)
    paths = service.team_paths(all_teams)
    return [
        TeamWithPath(**TeamOut.model_validate(t).model_dump(), path=paths[t.id])
        for t in all_teams
    ]


@router.post("", status_code=201)
async def create_team(ctx: CtxDep, data: TeamIn) -> TeamOut:
    fields = data.model_dump()
    _weight_to_decimal(fields)
    team = await service.create(ctx.session, ctx.actor, **fields)
    return TeamOut.model_validate(team)


@router.get("/{team_id}")
async def get_team(ctx: CtxDep, team_id: int, as_of: AsOf) -> TeamOut:
    team = await service.get(ctx.session, team_id, at=as_of)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    return TeamOut.model_validate(team)


@router.patch("/{team_id}")
async def update_team(ctx: CtxDep, team_id: int, data: TeamPatch) -> TeamOut:
    fields = data.model_dump(exclude_unset=True)
    if fields.pop("clear_parent", False):
        fields["parent_team_id"] = None
    if fields.pop("clear_workload_weight", False):
        fields["workload_weight"] = Decimal(0)  # 0 IS unweighted
    else:
        _weight_to_decimal(fields)
    team = await service.update(ctx.session, ctx.actor, team_id, **fields)
    return TeamOut.model_validate(team)


@router.delete("/{team_id}", status_code=204)
async def delete_team(ctx: CtxDep, team_id: int) -> None:
    await service.delete(ctx.session, ctx.actor, team_id)


@router.patch("/{team_id}/home-doc")
async def set_home_doc(ctx: CtxDep, team_id: int, data: HomeDocPatch) -> TeamOut:
    """Set (or clear, with url=null) the public Google Doc behind the team's
    /ministries/ page. Unlike PATCH /teams/{id}, this is open to the team's
    leaders, seconds and core members.

    Core members are included **deliberately**, and it is not an oversight to
    be tightened: ministry leaders here are often elderly, and a public page
    nobody can refresh goes stale. Widening the group that may keep it current
    is worth more than narrowing who may speak for the ministry — the content
    is nh3-sanitized and the URL must live on docs.google.com, so the exposure
    is what the page says, under a name the parish can correct."""
    team = await page_service.set_home_doc_url(
        ctx.session, ctx.actor, team_id, data.url
    )
    return TeamOut.model_validate(team)


@router.patch("/{team_id}/application-form")
async def set_application_form(
    ctx: CtxDep, team_id: int, data: ApplicationFormPatch
) -> TeamOut:
    """Set (or clear, with url=null) the Google Form mailed to people who ask
    about this ministry from its public page.

    Same audience as the home-page doc, and the same reason: both are how the
    team speaks to strangers. Only Google Forms links are accepted — the URL is
    mailed verbatim to whatever address a public form submitter typed, so an
    arbitrary one would make the parish a redirector."""
    team = await service.set_application_form_url(
        ctx.session, ctx.actor, team_id, data.url
    )
    return TeamOut.model_validate(team)


@router.get("/{team_id}/roster")
async def team_roster(ctx: CtxDep, team_id: int, as_of: AsOf) -> list[RosterEntry]:
    team = await service.get(ctx.session, team_id, at=as_of)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    rows = await service.roster(ctx.session, ctx.actor, team_id, at=as_of)
    full = ctx.actor.can_view_full_roster(team_id)
    manage = ctx.actor.can_manage_team(team_id)
    entries = []
    for membership, volunteer in rows:
        if full:
            out = VolunteerOut.model_validate(volunteer)
            if not manage:
                out.notes = None
        else:
            out = redacted(ctx.actor, volunteer, {team_id})
        entries.append(
            RosterEntry(
                membership_id=membership.id,
                volunteer=out,
                role=membership.role,
                role_label=role_label(membership.role),
            )
        )
    return entries


# --- interest submissions and the public page ---------------------------------
#
# The public /ministries/ page has always had a form and the team page has
# always listed what it collected; neither reached JSON. The form itself stays
# GUI-only on purpose — its submitter has no account, and what bounds it is a
# honeypot and a throttle rather than a permission.


@router.get("/{team_id}/interest")
async def list_interest(ctx: CtxDep, team_id: int) -> list[InterestOut]:
    """Open "I'm interested" submissions from this team's public page.

    Manage rights: each one carries a stranger's name, address, phone and free
    text, addressed to this ministry's leadership. Resolved ones are not listed —
    the list is a to-do, and `resolved_at` is how one leaves it."""
    rows = await interest_service.unresolved(ctx.session, ctx.actor, team_id)
    return [InterestOut.model_validate(r) for r in rows]


@router.post("/{team_id}/interest/{interest_id}/resolve")
async def resolve_interest(ctx: CtxDep, team_id: int, interest_id: int) -> InterestOut:
    """Mark a submission handled — the person was contacted, the form sent, or
    it was a bot. Resolving frees the (team, address) pair, so the same person
    can express interest again later."""
    interest = await interest_service.resolve(
        ctx.session, ctx.actor, interest_id, resolved_by=ctx.actor.user.id
    )
    if interest.team_id != team_id:
        raise LookupError(f"interest {interest_id} is not on team {team_id}")
    return InterestOut.model_validate(interest)


@router.get("/{team_id}/page")
async def get_team_page(ctx: CtxDep, team_id: int) -> TeamPageOut | None:
    """Whether the team's public page is publishing, and when it last fetched.

    Null when the team has no home doc set. The page's HTML is not here: it is
    served to the world at /ministries/<slug>.html, and what a caller needs from
    JSON is whether the nightly fetch is working."""
    page = await page_service.page_status(ctx.session, ctx.actor, team_id)
    return None if page is None else TeamPageOut.model_validate(page)


@router.post("/{team_id}/page/fetch")
async def fetch_team_page(ctx: CtxDep, team_id: int) -> TeamPageOut:
    """Refetch the team's doc now, instead of waiting for the nightly job.

    Same rights as setting the URL — core members included, deliberately, so a
    ministry is not blocked on one person to keep its public page current. A
    failed fetch keeps the last good page and reports itself in `status`, rather
    than blanking what the world can see."""
    team = await service.get(ctx.session, team_id)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    if not team.home_doc_url:
        raise ValueError("this team has no home page doc")
    async with httpx.AsyncClient() as http:
        page = await page_service.fetch_and_store(
            ctx.session, team, http, force=True, actor=ctx.actor
        )
    return TeamPageOut.model_validate(page)
