"""Public, no-login ministry home pages: /ministries/ (index) and
/ministries/<slug>.html (one per team, from the cached Google Doc HTML).

Raw FastAPI routes rather than NiceGUI pages: anonymous visitors get plain
HTML with no session or websocket. The /ministries prefix is exempted from
AuthMiddleware via UNRESTRICTED_PREFIXES in main.py; nothing here may read
the session or expose anything beyond team_page content and team names.
"""

from html import escape

import sqlalchemy as sa
from fastapi import HTTPException
from nicegui import app
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..db import db_session
from ..models import Team, TeamPageImage
from ..services import pages as page_service
from ..services import teams as team_service
from .assets import static_url

CACHE_HEADERS = {"Cache-Control": "public, max-age=300"}

# The shell links /static/theme.css (the app's neo-greco theme: body font +
# background, link colors, .vdb-header, .vdb-brand, .vdb-page-title with its
# meander). Only public-shell-specific layout lives here, every color a theme
# token — the pages stay light-mode (the theme's dark tokens hang off Quasar's
# body--dark class, which never exists on this plain-HTML surface).
_SHELL_CSS = """
  body { margin: 0; }
  header.vdb-header { padding: 0.7rem 1.2rem; }
  header.vdb-header a { color: white; font-size: 1.1rem; }
  header.vdb-header a:hover { border-bottom: none; }
  main { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1.2rem 3rem; }
  h1.vdb-page-title { font-size: 1.7rem; margin: 0.5rem 0 0.8rem; }
  .meta { color: var(--vdb-heading); opacity: 0.75; font-size: 0.85rem;
          margin: 0.3rem 0 1.5rem; }
  .doc { background: var(--vdb-surface); border: 1px solid var(--vdb-rule);
         border-radius: 4px; padding: 2rem; overflow-x: auto; }
  .doc img { max-width: 100%; height: auto; }
  ul.index { list-style: none; padding: 0; }
  ul.index li { margin: 0.6rem 0; }
  ul.index a { font-size: 1.1rem; }
"""


def _shell(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        '<link rel="preload" href="/static/fonts/cinzel-v11-latin-regular.woff2"'
        ' as="font" type="font/woff2" crossorigin>\n'
        f'<link rel="stylesheet" href="{static_url("theme.css")}">\n'
        f"<style>{_SHELL_CSS}</style></head>\n"
        '<body><header class="vdb-header">'
        '<a class="vdb-brand" href="/ministries/">⛪ Ministries</a></header>\n'
        f"<main>{body}</main></body></html>\n"
    )


def _title(text: str) -> str:
    return f'<h1 class="vdb-page-title">{escape(text)}</h1>'


def _not_found() -> HTMLResponse:
    body = (
        f"{_title('Page not found')}"
        '<p>This ministry has no published home page. The <a href="/ministries/">'
        "ministries index</a> lists every published page.</p>"
    )
    return HTMLResponse(_shell("Not found", body), status_code=404)


def register() -> None:
    """Wire the routes onto the global app. Called from register_pages() on
    every create_app() rather than at import time: test simulations wipe the
    app's routes and re-run create_app() with this module already cached, so
    an import-time decorator would fire only once per process."""
    app.get("/ministries", include_in_schema=False)(ministries_redirect)
    app.get("/ministries/", include_in_schema=False)(ministries_index)
    app.get("/ministries/{slug}.html", include_in_schema=False)(ministry_page)
    app.get("/ministries/img/{team_id}/{seq}", include_in_schema=False)(ministry_image)


async def ministries_redirect() -> RedirectResponse:
    return RedirectResponse("/ministries/")


async def ministries_index() -> HTMLResponse:
    async with db_session() as session:
        published = await page_service.published_teams(session)
        all_teams = await team_service.list_all(session)
    paths = team_service.team_paths(all_teams)
    slugs = page_service.slug_map(paths)
    items = sorted(
        (paths.get(team.id, team.name), slugs[team.id])
        for team in published
        if team.id in slugs
    )
    if items:
        listing = "".join(
            f'<li><a href="/ministries/{escape(slug)}.html">{escape(path)}</a></li>'
            for path, slug in items
        )
        body = f"{_title('Ministry home pages')}<ul class='index'>{listing}</ul>"
    else:
        body = (
            f"{_title('Ministry home pages')}"
            "<p>No ministry has published a home page yet.</p>"
        )
    return HTMLResponse(_shell("Ministries", body), headers=CACHE_HEADERS)


async def _resolve_slug(session, slug: str) -> tuple[Team | None, dict[int, str]]:
    """The team behind a public slug (or None) plus every team's display path."""
    all_teams = await team_service.list_all(session)
    paths = team_service.team_paths(all_teams)
    slugs = page_service.slug_map(paths)
    team = next((t for t in all_teams if slugs.get(t.id) == slug), None)
    return team, paths


async def ministry_page(slug: str) -> HTMLResponse:
    async with db_session() as session:
        team, paths = await _resolve_slug(session, slug)
        # one row for the one team — never every published page's html
        page = (
            await page_service.published_page(session, team.id)
            if team is not None
            else None
        )
    if page is None:
        return _not_found()
    title = paths[team.id]
    updated = (
        f'<p class="meta">Last updated {page.fetched_at:%B %-d, %Y}</p>'
        if page.fetched_at
        else ""
    )
    body = f'{_title(title)}{updated}<div class="doc">{page.html}</div>'
    return HTMLResponse(_shell(f"{title} — Ministries", body), headers=CACHE_HEADERS)


async def ministry_image(team_id: int, seq: int, v: str | None = None) -> Response:
    """An image cached from the team's doc (services.pages._localize_images).
    Joined to the team's published state so that unpublishing or deactivating
    a team takes its images offline with it.

    `v` is the content hash _localize_images bakes into the page html: a
    hashed URL names exact bytes, so it is served immutable (the photos-route
    pattern). Bare URLs — html cached before hashing shipped — keep the short
    lifetime."""
    async with db_session() as session:
        row = (
            await session.execute(
                sa.select(TeamPageImage)
                .join(Team, Team.id == TeamPageImage.team_id)
                .where(
                    TeamPageImage.team_id == team_id,
                    TeamPageImage.seq == seq,
                    Team.is_active,
                    Team.home_doc_url.is_not(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "no such image")
    headers = (
        {"Cache-Control": "public, max-age=31536000, immutable"} if v else CACHE_HEADERS
    )
    return Response(content=row.image, media_type=row.content_type, headers=headers)
