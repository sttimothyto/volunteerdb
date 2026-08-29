"""Team files as plain GET routes: the parish or my-teams CSV, one team's
roster CSV (as-of aware), the roster template, and a team page's QR code.

These were ui.download() calls — bytes pushed over the websocket from a
button handler — which meant no right-click-save, no curl, nothing without
a live connection, and a button that did nothing until the socket came
back. A download on the web is a link to an address that answers with
Content-Disposition, so that is what they are now; the buttons carry an
href and render as <a>. Cookie-authenticated like /photos, and every check
the handlers made is re-made here inside the session: the rendered gate is
not the gate, and the exporter does its own authorization besides.
"""

from fastapi import HTTPException, Request
from nicegui import app
from starlette.responses import Response

from ..api.deps import gate, raise_http
from ..db import db_session
from ..models import Team
from ..services import pages as page_service
from ..services import teams as team_service
from ..sheets import exporter
from .context import get_actor, parse_as_of

CSV = "text/csv; charset=utf-8"


def _attachment(content: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


async def teams_export(request: Request) -> Response:
    """Every team an admin may see, or the union of the teams the reader has
    full-roster rights on (full_view_team_ids: core members export too, and
    load_actor has already taken the task forces out — a borrowed roster is
    not yours to export)."""
    async with db_session() as session:
        actor = await get_actor(session)
        if actor is None:
            raise HTTPException(401, "sign in to export")
        if actor.is_admin:
            scope, name = None, "parish"
        else:
            gate(bool(actor.full_view_team_ids), "export your teams")
            scope, name = actor.full_view_team_ids, "my-teams"
        content = raise_http(await exporter.export_csv(session, actor, team_ids=scope))
    return _attachment(content, f"volunteerdb-{name}.csv", CSV)


async def roster_export(team_id: int, request: Request, as_of: str = "") -> Response:
    """One team's roster, at the moment `?as_of=` names or now."""
    at = parse_as_of(as_of)
    async with db_session() as session:
        actor = await get_actor(session)
        if actor is None:
            raise HTTPException(401, "sign in to export")
        team = await session.get(Team, team_id)
        if team is None:
            raise HTTPException(404, "no such team")
        content = raise_http(
            await exporter.export_csv(session, actor, team_id=team_id, at=at)
        )
    stem = team.name.lower().replace(" ", "-")
    return _attachment(content, f"{stem}.csv", CSV)


async def roster_template(request: Request) -> Response:
    async with db_session() as session:
        if await get_actor(session) is None:
            raise HTTPException(401, "sign in to download the template")
    return _attachment(exporter.template_csv(), "volunteerdb-template.csv", CSV)


async def page_qr(team_id: int, request: Request) -> Response:
    """A QR code for the team's public page — print-usable, and only for a
    page that is actually published; the address inside it is public."""
    base_url = str(request.base_url).rstrip("/")
    async with db_session() as session:
        if await get_actor(session) is None:
            raise HTTPException(401, "sign in to download the QR code")
        paths = (await team_service.tree(session)).paths
        slug = page_service.slug_map(paths).get(team_id)
        if slug is None or not await page_service.is_published(session, team_id):
            raise HTTPException(404, "this team has no published page")
    png = page_service.qr_png(f"{base_url}/ministries/{slug}.html")
    return _attachment(png, f"{slug}-qr.png", "image/png")


def register() -> None:
    """Wire the routes onto the global app; called from register_pages() on
    every create_app() (see ministries_routes.register for why)."""
    # under /export, not /teams: the /teams/{team_id} page route is registered
    # first and would claim "export.csv" as a team id (a 422, not a file)
    app.get("/export/teams.csv", include_in_schema=False)(teams_export)
    app.get("/export/roster-template.csv", include_in_schema=False)(roster_template)
    app.get("/teams/{team_id}/roster.csv", include_in_schema=False)(roster_export)
    app.get("/teams/{team_id}/qr.png", include_in_schema=False)(page_qr)
