"""Public, no-login ministry home pages: /ministries/ (index) and
/ministries/<slug>.html (one per team, from the cached Google Doc HTML).

Raw FastAPI routes rather than NiceGUI pages: anonymous visitors get plain
HTML with no session or websocket. The /ministries prefix is exempted from
AuthMiddleware via UNRESTRICTED_PREFIXES in main.py; nothing here may read
the session or expose anything beyond team_page content and team names.
"""

from html import escape

import sqlalchemy as sa
from fastapi import HTTPException, Request
from nicegui import app
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .. import throttle
from ..db import db_session
from ..log import audit_log
from ..models import Team, TeamPageImage
from ..services import interest as interest_service
from ..services import mail
from ..services import pages as page_service
from ..services import teams as team_service
from .assets import static_url

CACHE_HEADERS = {"Cache-Control": "public, max-age=300"}

# per-IP and per-target-email caps on interest submissions: the form is public
# and each accepted submission emails the typed address, so the email key is
# what bounds the spam-relay blast radius toward any one mailbox
INTEREST_IP_LIMIT = (5, 3600.0)
INTEREST_EMAIL_LIMIT = (3, 86400.0)

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
  .interest { background: var(--vdb-surface); border: 1px solid var(--vdb-rule);
              border-radius: 4px; padding: 1.5rem 2rem; margin-top: 1.5rem; }
  .interest h2 { font-size: 1.2rem; margin: 0 0 0.3rem; }
  .interest label { display: block; margin: 0.7rem 0 0.2rem; font-size: 0.9rem; }
  .interest input, .interest textarea {
    width: 100%; box-sizing: border-box; padding: 0.4rem 0.5rem;
    border: 1px solid var(--vdb-rule); border-radius: 3px; font: inherit; }
  .interest button { margin-top: 1rem; padding: 0.45rem 1.2rem; font: inherit;
                     cursor: pointer; }
  /* honeypot: humans never see it, form-spam bots fill it */
  .interest .hp { position: absolute; left: -9999px; }
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
    app.post("/ministries/{slug}/interest", include_in_schema=False)(ministry_interest)


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


def _interest_section(slug: str, team_name: str, sent: bool) -> str:
    """The "I'm interested" form under the doc — or, after a submit, its
    confirmation. Static HTML, so the page stays cacheable (?sent=1 is its
    own cache key). The `website` field is a honeypot the POST handler checks."""
    if sent:
        return (
            '<div class="interest"><h2>Thank you!</h2>'
            f"<p>Thank you for your interest in {escape(team_name)} — watch "
            "your email for the next steps. (If you had already sent a note "
            "recently, we won't email you twice.)</p></div>"
        )
    return (
        '<div class="interest"><h2>Interested in joining?</h2>'
        "<p>Leave your details and the ministry leaders will get in touch.</p>"
        f'<form method="post" action="/ministries/{escape(slug)}/interest">'
        '<div class="hp" aria-hidden="true"><label>Website'
        '<input name="website" tabindex="-1" autocomplete="off"></label></div>'
        '<label>Name<input name="name" required maxlength="200"></label>'
        '<label>Email<input name="email" type="email" required maxlength="255">'
        "</label>"
        '<label>Phone (optional)<input name="phone" maxlength="50"></label>'
        "<label>Anything you'd like us to know? (optional)"
        '<textarea name="note" rows="3" maxlength="2000"></textarea></label>'
        '<button type="submit">I\'m interested</button>'
        "</form></div>"
    )


async def ministry_page(slug: str, sent: str = "") -> HTMLResponse:
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
    body = (
        f'{_title(title)}{updated}<div class="doc">{page.html}</div>'
        f"{_interest_section(slug, team.name, bool(sent))}"
    )
    return HTMLResponse(_shell(f"{title} — Ministries", body), headers=CACHE_HEADERS)


async def ministry_interest(slug: str, request: Request) -> Response:
    """Public form target. Every outcome — stored, honeypot, throttled,
    duplicate — redirects to the same thank-you page: a public endpoint that
    revealed which submissions were kept would be an enumeration oracle."""
    form = await request.form()
    ip = request.client.host if request.client else "unknown"
    email = str(form.get("email", "")).strip().lower()
    done = RedirectResponse(
        f"/ministries/{slug}.html?sent=1",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    if str(form.get("website", "")):
        audit_log("interest.honeypot", slug=slug, ip=ip)
        return done
    if throttle.blocked(f"interest-ip:{ip}", *INTEREST_IP_LIMIT) or (
        email and throttle.blocked(f"interest-email:{email}", *INTEREST_EMAIL_LIMIT)
    ):
        audit_log("interest.throttled", slug=slug, ip=ip)
        return done
    throttle.hit(f"interest-ip:{ip}")
    if email:
        throttle.hit(f"interest-email:{email}")

    async with db_session() as session:
        team, paths = await _resolve_slug(session, slug)
        published = (
            await page_service.published_page(session, team.id)
            if team is not None
            else None
        )
        if team is None or published is None:
            return _not_found()
        try:
            interest = await interest_service.submit(
                session,
                team_id=team.id,
                name=str(form.get("name", "")),
                email=email,
                phone=str(form.get("phone", "")) or None,
                note=str(form.get("note", "")) or None,
            )
        except ValueError:  # missing name / unusable email — bots, mostly
            audit_log("interest.invalid", slug=slug, ip=ip)
            return done
        if interest is None:  # open duplicate: keep quiet, mail nobody again
            audit_log("interest.duplicate", slug=slug, team_id=team.id, ip=ip)
            return done
        leaders = await interest_service.leader_emails(session, team.id)
        team_id = team.id
        team_name = team.name
        team_path = paths[team.id]
        form_url = team.application_form_url
        stored_name, stored_email, stored_phone, stored_note = (
            interest.name,
            interest.email,
            interest.phone,
            interest.note,
        )
        team_url = f"{str(request.base_url).rstrip('/')}/teams/{team.id}"

    # mail only after the transaction committed; send_email never raises
    for leader in leaders:
        await mail.send_email(
            leader,
            *mail.interest_leader_email(
                team_path,
                stored_name,
                stored_email,
                stored_phone,
                stored_note,
                team_url,
            ),
        )
    await mail.send_email(
        stored_email, *mail.interest_applicant_email(team_name, form_url)
    )
    audit_log("interest.submitted", team_id=team_id, ip=ip)
    return done


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
