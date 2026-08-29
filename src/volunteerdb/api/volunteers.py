from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, UploadFile
from starlette.responses import Response

from ..models import Volunteer
from ..permissions import Actor, team_ids_map, volunteer_team_ids
from ..services import custom_fields as custom_field_service
from ..services import elections as elections_service
from ..services import events as event_service
from ..services import photos as photo_service
from ..services import users as user_service
from ..services import volunteers as service
from .deps import AsOf, CtxDep, dispatch, gate, raise_http
from .elections import proposal_out
from .schemas import (
    AssignmentOut,
    ImpactOut,
    InvolvementOut,
    PhotoMetaOut,
    TimelineSegmentOut,
    TimelineSpellOut,
    UserOut,
    VolunteerHoursOut,
    VolunteerIn,
    VolunteerOut,
    VolunteerPatch,
    role_label,
)

router = APIRouter(prefix="/volunteers", tags=["volunteers"])


def redacted(actor: Actor, volunteer: Volunteer, team_ids: set[int]) -> VolunteerOut:
    """Everyone may see names; contact details, notes and custom values need closer ties."""
    out = VolunteerOut.model_validate(volunteer)
    if not actor.can_view_volunteer(volunteer.id, team_ids):
        out.email = out.phone = out.notes = out.custom = None
    elif not actor.can_edit_volunteer(volunteer.id, team_ids):
        out.notes = None
    return out


@router.get("")
async def list_volunteers(
    ctx: CtxDep, as_of: AsOf, q: str = "", include_inactive: bool = False
) -> list[VolunteerOut]:
    """Name/contact search, or a filter expression.

    `q` is plain substring text unless it parses as a SQL boolean condition —
    `phone LIKE \'555%\' AND team = \'Liturgy\'` — in which case it runs as a
    filter (see the query-language reference). Either way it matches names for
    everyone and contact fields only among volunteers the caller may read
    unredacted; a filter naming an unknown field is a 422 rather than a silent
    fallback to text search."""
    if include_inactive:
        # matches the GUI, which offers the archived toggle to admins only
        gate(ctx.actor.is_admin, "only admins list archived volunteers")
    # search_or_query, not search: `q` accepts the same SQL-shaped filter the
    # GUI's search boxes take (query_lang), and the grammar is already
    # actor-scoped per field. Plain text still means a substring search.
    found = raise_http(
        await service.search_or_query(
            ctx.session, q, at=as_of, include_inactive=include_inactive, actor=ctx.actor
        )
    )
    teams_map = await team_ids_map(ctx.session, [v.id for v in found], as_of)
    photo_ids = await photo_service.versions(ctx.session, [v.id for v in found])
    out = [redacted(ctx.actor, v, teams_map.get(v.id, set())) for v in found]
    for entry in out:
        entry.has_photo = entry.id in photo_ids
    return out


@router.post("", status_code=201)
async def create_volunteer(ctx: CtxDep, data: VolunteerIn) -> VolunteerOut:
    volunteer = raise_http(
        await service.create(ctx.session, ctx.actor, **data.model_dump())
    )
    return VolunteerOut.model_validate(volunteer)


@router.get("/{volunteer_id}")
async def get_volunteer(ctx: CtxDep, volunteer_id: int, as_of: AsOf) -> VolunteerOut:
    volunteer = await service.get(ctx.session, volunteer_id, at=as_of)
    if volunteer is None:
        raise HTTPException(404, f"volunteer {volunteer_id} not found")
    team_ids = (await team_ids_map(ctx.session, [volunteer_id], as_of))[volunteer_id]
    out = redacted(ctx.actor, volunteer, team_ids)
    out.has_photo = bool(await photo_service.versions(ctx.session, [volunteer_id]))
    return out


@router.patch("/{volunteer_id}")
async def update_volunteer(
    ctx: CtxDep,
    volunteer_id: int,
    data: VolunteerPatch,
    request: Request,
    background: BackgroundTasks,
) -> VolunteerOut:
    team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
    fields = data.model_dump(exclude_unset=True)
    if "email" in fields and volunteer_id == ctx.actor.volunteer_id:
        # Your own address is also what you sign in with, so moving it to a NEW
        # address needs the confirm round-trip — and this API sends no email
        # (the precedent api/events.py states), so it cannot run that exchange.
        # Two writes are still fine: no change at all, and syncing your record
        # onto the address you ALREADY sign in with (already confirmed — the one
        # way to fill a linked record whose email is blank). Everyone else's
        # address is a plain edit.
        on_file = await service.get(ctx.session, volunteer_id)
        typed = (fields["email"] or "").strip().lower()
        current = (on_file.email or "").strip().lower() if on_file else ""
        own_login = (ctx.actor.user.email or "").strip().lower()
        if typed != current and typed != own_login:
            raise HTTPException(
                422,
                "your own address changes only once the new one confirms "
                "itself; ask for it on the Password & sign-in page (/account) "
                "and we will mail a confirmation link there",
            )
    # somebody else's address moving is worth a word to the address it moved
    # away from (the service's AddressReplaced event): the notice runs after
    # the commit, so the acting session cannot suppress it
    custom = fields.pop("custom", None)
    volunteer = dispatch(
        ctx,
        background,
        await service.update(ctx.session, ctx.actor, volunteer_id, **fields),
    )
    if custom is not None:
        volunteer = raise_http(
            await custom_field_service.set_values(
                ctx.session, ctx.actor, volunteer_id, custom
            )
        )
    return redacted(ctx.actor, volunteer, team_ids)


@router.delete("/{volunteer_id}", status_code=204)
async def delete_volunteer(ctx: CtxDep, volunteer_id: int) -> None:
    raise_http(await service.delete(ctx.session, ctx.actor, volunteer_id))


@router.put("/{volunteer_id}/photo")
async def put_photo(ctx: CtxDep, volunteer_id: int, file: UploadFile) -> PhotoMetaOut:
    """Upload/replace the headshot. Open to every signed-in account by design
    (deliberate exception to can_edit_volunteer); stored normalized to a
    400x400 JPEG."""
    content = await file.read(photo_service.MAX_UPLOAD_BYTES + 1)
    if len(content) > photo_service.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image file larger than 10 MB")
    record = raise_http(
        await photo_service.set_photo(
            ctx.session,
            volunteer_id,
            content,
            uploaded_by=ctx.actor.user.id,
            now=ctx.now,
        )
    )
    return PhotoMetaOut(
        volunteer_id=record.volunteer_id,
        content_type=record.content_type,
        size_bytes=len(record.image),
        uploaded_at=record.uploaded_at,
    )


@router.get("/{volunteer_id}/photo")
async def get_photo(ctx: CtxDep, volunteer_id: int) -> Response:
    """The stored JPEG bytes. Visible to all signed-in users."""
    record = await photo_service.get(ctx.session, volunteer_id)
    if record is None:
        raise HTTPException(404, f"volunteer {volunteer_id} has no photo")
    return Response(content=record.image, media_type=record.content_type)


@router.delete("/{volunteer_id}/photo", status_code=204)
async def delete_photo(ctx: CtxDep, volunteer_id: int) -> None:
    """Remove the headshot (idempotent). Open to every signed-in account."""
    raise_http(await photo_service.delete_photo(ctx.session, volunteer_id))


@router.get("/{volunteer_id}/assignments")
async def volunteer_assignments(
    ctx: CtxDep, volunteer_id: int, as_of: AsOf
) -> list[AssignmentOut]:
    """Which teams does this person serve on? Visible to all signed-in users."""
    rows = await service.assignments(ctx.session, volunteer_id, at=as_of)
    return [
        AssignmentOut(
            membership_id=m.id, team=t, role=m.role, role_label=role_label(m.role)
        )
        for m, t in rows
    ]


@router.get("/{volunteer_id}/timeline")
async def volunteer_timeline(ctx: CtxDep, volunteer_id: int) -> list[TimelineSpellOut]:
    """Membership spells over all time, stitched from the audit trail.

    Inherently all-time, so no as_of param. Visible to all signed-in users,
    like /assignments.
    """
    spells = await service.timeline(ctx.session, volunteer_id)
    return [
        TimelineSpellOut(
            team_id=s.team_id,
            team_name=s.team_name,
            team_deleted=s.team_deleted,
            role=s.role,
            role_label=role_label(s.role),
            start=s.start,
            end=s.end,
            segments=[
                TimelineSegmentOut(
                    role=seg.role,
                    role_label=role_label(seg.role),
                    start=seg.start,
                    end=seg.end,
                )
                for seg in s.segments
            ],
        )
        for s in spells
    ]


@router.post("/{volunteer_id}/invite")
async def invite_volunteer(
    ctx: CtxDep, volunteer_id: int, request: Request, background: BackgroundTasks
) -> UserOut:
    """Create the sign-in account this volunteer does not have yet, with its
    invite link armed, and return it.

    Open to admins and to leaders/seconds/core of a team the volunteer is on —
    the GUI counterpart is the roster's invite control. Unlike the admin
    endpoints under /api/users this is scoped to one volunteer and only ever
    mints a non-admin account linked to them.

    **Only an admin is given `invite_token` back.** The link is a bearer
    credential — whoever holds it signs in as that volunteer — and a leader may
    add anybody to their own team and then edit their address, which turned
    "invite my team member" into "take over any account that has never signed
    in". For a non-admin caller the link is instead mailed to the address on
    the volunteer's own record, and the response carries the account without
    the token. This is the one place the API sends mail (the rule stated in
    api/events.py): the alternative is minting a credential that reaches
    nobody. It rides a background task, so it goes out after the commit.

    422 when the volunteer is archived, has no email, or already has a working
    account.
    """
    team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
    gate(ctx.actor.can_invite_volunteer(team_ids), "invite this volunteer")
    # an admin gets the link in the body, so the send is dropped (silent);
    # anybody else's link goes out by mail, after the commit
    account, token = dispatch(
        ctx,
        background,
        await user_service.invite_volunteer(
            ctx.session, volunteer_id, invite=ctx.env.invite()
        ),
        silent=ctx.actor.is_admin,
    )
    out = UserOut.model_validate(account)
    out.has_password = account.password_hash is not None
    # never off the row — the column holds only a digest (services.users)
    out.invite_token = token if ctx.actor.is_admin else None
    return out


@router.get("/{volunteer_id}/proposals")
async def volunteer_proposals(ctx: CtxDep, volunteer_id: int) -> list[InvolvementOut]:
    """Proposals where this volunteer is a candidate, a voting member, or the
    appointee. Elections are live-only, so no as_of. Access and scoping mirror
    GET /elections/proposals: admins see all, managers their subtree, voters
    the rolls they sit on."""
    gate(ctx.actor.can_access_elections, "use the elections page")
    rows = await elections_service.involving(
        ctx.session, ctx.actor, volunteer_id, today=ctx.env.today()
    )
    return [
        InvolvementOut(
            proposal=proposal_out(r.proposal, today=ctx.env.today()),
            path=r.path,
            as_candidate=r.as_candidate,
            as_voter=r.as_voter,
            appointed=r.appointed,
        )
        for r in rows
    ]


@router.get("/{volunteer_id}/hours")
async def volunteer_hours(ctx: CtxDep, volunteer_id: int) -> VolunteerHoursOut:
    """Derived service record: hours over past, non-cancelled events the
    volunteer was assigned to (auto = scheduled duration, unless a manager
    recorded an exception). Visible to whoever may view the full profile."""
    if await service.get(ctx.session, volunteer_id) is None:
        raise HTTPException(404, f"volunteer {volunteer_id} not found")
    summary = raise_http(
        await event_service.hours_for_volunteer(
            ctx.session, ctx.actor, volunteer_id, now=ctx.now
        )
    )
    return VolunteerHoursOut(
        volunteer_id=volunteer_id,
        total_hours=float(summary.total_hours),
        events_attended=summary.events_attended,
    )


@router.get("/{volunteer_id}/impact")
async def volunteer_impact(
    ctx: CtxDep, volunteer_id: int, as_of: AsOf
) -> list[ImpactOut]:
    """If this volunteer leaves, what holes appear?"""
    rows = raise_http(
        await service.impact(ctx.session, ctx.actor, volunteer_id, at=as_of)
    )
    return [
        ImpactOut(
            team=r.team,
            role=r.role,
            role_label=role_label(r.role),
            leaders_left=r.leaders_left,
            leadership_left=r.leadership_left,
        )
        for r in rows
    ]
