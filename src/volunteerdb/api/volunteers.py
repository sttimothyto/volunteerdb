from fastapi import APIRouter, HTTPException, UploadFile
from starlette.responses import Response

from ..models import Volunteer
from ..permissions import Actor, require, team_ids_map, volunteer_team_ids
from ..services import custom_fields as custom_field_service
from ..services import elections as elections_service
from ..services import events as event_service
from ..services import photos as photo_service
from ..services import volunteers as service
from .deps import AsOf, CtxDep
from .elections import proposal_out
from .schemas import (
    AssignmentOut,
    ImpactOut,
    InvolvementOut,
    PhotoMetaOut,
    TimelineSegmentOut,
    TimelineSpellOut,
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
    if include_inactive:
        # matches the GUI, which offers the archived toggle to admins only
        require(ctx.actor.is_admin, "only admins list archived volunteers")
    found = await service.search(
        ctx.session, q, at=as_of, include_inactive=include_inactive, actor=ctx.actor
    )
    teams_map = await team_ids_map(ctx.session, [v.id for v in found], as_of)
    photo_ids = await photo_service.versions(ctx.session, [v.id for v in found])
    out = [redacted(ctx.actor, v, teams_map.get(v.id, set())) for v in found]
    for entry in out:
        entry.has_photo = entry.id in photo_ids
    return out


@router.post("", status_code=201)
async def create_volunteer(ctx: CtxDep, data: VolunteerIn) -> VolunteerOut:
    require(ctx.actor.is_admin, "only admins create volunteers")
    volunteer = await service.create(ctx.session, **data.model_dump())
    return VolunteerOut.model_validate(volunteer)


@router.get("/{volunteer_id}")
async def get_volunteer(ctx: CtxDep, volunteer_id: int, as_of: AsOf) -> VolunteerOut:
    volunteer = await service.get(ctx.session, volunteer_id, at=as_of)
    if volunteer is None:
        raise LookupError(f"volunteer {volunteer_id} not found")
    team_ids = (await team_ids_map(ctx.session, [volunteer_id], as_of))[volunteer_id]
    out = redacted(ctx.actor, volunteer, team_ids)
    out.has_photo = bool(await photo_service.versions(ctx.session, [volunteer_id]))
    return out


@router.patch("/{volunteer_id}")
async def update_volunteer(
    ctx: CtxDep, volunteer_id: int, data: VolunteerPatch
) -> VolunteerOut:
    team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
    require(ctx.actor.can_edit_volunteer(volunteer_id, team_ids), "edit this volunteer")
    fields = data.model_dump(exclude_unset=True)
    if "is_active" in fields:
        require(ctx.actor.is_admin, "only admins archive volunteers")
    custom = fields.pop("custom", None)
    volunteer = await service.update(ctx.session, volunteer_id, **fields)
    if custom is not None:
        volunteer = await custom_field_service.set_values(
            ctx.session, volunteer_id, custom
        )
    return redacted(ctx.actor, volunteer, team_ids)


@router.delete("/{volunteer_id}", status_code=204)
async def delete_volunteer(ctx: CtxDep, volunteer_id: int) -> None:
    require(ctx.actor.is_admin, "only admins delete volunteers")
    await service.delete(ctx.session, volunteer_id)


@router.put("/{volunteer_id}/photo")
async def put_photo(ctx: CtxDep, volunteer_id: int, file: UploadFile) -> PhotoMetaOut:
    """Upload/replace the headshot. Open to every signed-in account by design
    (deliberate exception to can_edit_volunteer); stored normalized to a
    400x400 JPEG."""
    content = await file.read(photo_service.MAX_UPLOAD_BYTES + 1)
    if len(content) > photo_service.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image file larger than 10 MB")
    record = await photo_service.set_photo(
        ctx.session, volunteer_id, content, uploaded_by=ctx.actor.user.id
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
        raise LookupError(f"volunteer {volunteer_id} has no photo")
    return Response(content=record.image, media_type=record.content_type)


@router.delete("/{volunteer_id}/photo", status_code=204)
async def delete_photo(ctx: CtxDep, volunteer_id: int) -> None:
    """Remove the headshot (idempotent). Open to every signed-in account."""
    await photo_service.delete_photo(ctx.session, volunteer_id)


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


@router.get("/{volunteer_id}/proposals")
async def volunteer_proposals(ctx: CtxDep, volunteer_id: int) -> list[InvolvementOut]:
    """Proposals where this volunteer is a candidate, a voting member, or the
    appointee. Elections are live-only, so no as_of. Access and scoping mirror
    GET /elections/proposals: admins see all, managers their subtree, voters
    the rolls they sit on."""
    require(ctx.actor.can_access_elections, "use the elections page")
    rows = await elections_service.involving(ctx.session, ctx.actor, volunteer_id)
    return [
        InvolvementOut(
            proposal=proposal_out(r.proposal),
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
        raise LookupError(f"volunteer {volunteer_id} not found")
    team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
    require(
        ctx.actor.can_view_volunteer(volunteer_id, team_ids),
        "view this volunteer's service record",
    )
    summary = await event_service.hours_for_volunteer(ctx.session, volunteer_id)
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
    team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
    require(
        ctx.actor.can_view_volunteer(volunteer_id, team_ids),
        "view this volunteer's impact",
    )
    rows = await service.impact(ctx.session, volunteer_id, at=as_of)
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
