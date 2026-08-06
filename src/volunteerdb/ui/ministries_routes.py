"""Public, no-login ministry home pages: /ministries/ (index) and
/ministries/<slug>.html (one per team, from the cached Google Doc HTML).

Raw FastAPI routes rather than NiceGUI pages: anonymous visitors get plain
HTML with no session or websocket. The /ministries prefix is exempted from
AuthMiddleware via UNRESTRICTED_PREFIXES in main.py; nothing here may read
the session or expose anything beyond team_page content and team names.
"""

from html import escape

from nicegui import app
from starlette.responses import HTMLResponse, RedirectResponse

from ..db import db_session
from ..services import pages as page_service
from ..services import teams as team_service

CACHE_HEADERS = {"Cache-Control": "public, max-age=300"}

_SHELL_CSS = """
  body { margin: 0; font-family: Georgia, 'Times New Roman', serif;
         background: #FAF6EF; color: #2A2622; }
  .bar { background: #2A2622; padding: 0.6rem 1.2rem; }
  .bar a { color: #E8DCC8; text-decoration: none; font-size: 1.05rem;
           letter-spacing: 0.04em; }
  main { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1.2rem 3rem; }
  .meta { color: #8A7550; font-size: 0.85rem; margin: 0.3rem 0 1.5rem; }
  .doc { background: #FFFFFF; border: 1px solid #E0D5C0; border-radius: 4px;
         padding: 2rem; overflow-x: auto; }
  .doc img { max-width: 100%; height: auto; }
  ul.index { list-style: none; padding: 0; }
  ul.index li { margin: 0.6rem 0; }
  ul.index a { color: #A5573E; font-size: 1.1rem; }
"""


def _shell(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_SHELL_CSS}</style></head>\n"
        '<body><div class="bar"><a href="/ministries/">⛪ Ministries</a></div>\n'
        f"<main>{body}</main></body></html>\n"
    )


def _not_found() -> HTMLResponse:
    body = (
        "<h1>Page not found</h1>"
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


async def ministries_redirect() -> RedirectResponse:
    return RedirectResponse("/ministries/")


async def ministries_index() -> HTMLResponse:
    async with db_session() as session:
        pairs = await page_service.published(session)
        all_teams = await team_service.list_all(session)
    paths = team_service.team_paths(all_teams)
    slugs = page_service.slug_map(paths)
    items = sorted(
        (paths.get(team.id, team.name), slugs.get(team.id), page)
        for team, page in pairs
        if team.id in slugs
    )
    if items:
        listing = "".join(
            f'<li><a href="/ministries/{escape(slug)}.html">{escape(path)}</a></li>'
            for path, slug, _page in items
        )
        body = f"<h1>Ministry home pages</h1><ul class='index'>{listing}</ul>"
    else:
        body = (
            "<h1>Ministry home pages</h1>"
            "<p>No ministry has published a home page yet.</p>"
        )
    return HTMLResponse(_shell("Ministries", body), headers=CACHE_HEADERS)


async def ministry_page(slug: str) -> HTMLResponse:
    async with db_session() as session:
        pairs = await page_service.published(session)
        all_teams = await team_service.list_all(session)
    paths = team_service.team_paths(all_teams)
    slugs = page_service.slug_map(paths)
    for team, page in pairs:
        if slugs.get(team.id) == slug:
            break
    else:
        return _not_found()
    title = paths.get(team.id, team.name)
    updated = (
        f'<p class="meta">Last updated {page.fetched_at:%B %-d, %Y}</p>'
        if page.fetched_at
        else ""
    )
    body = f'<h1>{escape(title)}</h1>{updated}<div class="doc">{page.html}</div>'
    return HTMLResponse(_shell(f"{title} — Ministries", body), headers=CACHE_HEADERS)
