"""Cookie-authenticated headshot images for the browser (panel, graph canvas).

Not under /api (that prefix skips AuthMiddleware and expects Bearer tokens):
<img src="/photos/{id}"> and cytoscape background-image loads send the session
cookie, so this path rides the normal signed-in session.
"""

from fastapi import HTTPException
from nicegui import app
from starlette.responses import Response

from ..db import db_session
from ..services import photos as photo_service
from .context import get_actor


@app.get("/photos/{volunteer_id}", include_in_schema=False)
async def photo(volunteer_id: int) -> Response:
    async with db_session() as session:
        # AuthMiddleware already bounced anonymous browsers; this also covers
        # deactivated accounts that still hold a cookie
        if await get_actor(session) is None:
            raise HTTPException(401, "sign in to view photos")
        record = await photo_service.get(session, volunteer_id)
        if record is None:
            raise HTTPException(404, "no photo")
        return Response(
            content=record.image,
            media_type=record.content_type,
            # the ?v= cache-buster changes on every replace, so cache hard
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )
