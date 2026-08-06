"""Team home pages: each team may point at one PUBLIC Google Doc, whose HTML
is fetched (nightly by jobs.fetch_pages, on demand from the team page),
sanitized, and cached in team_page for the no-login /ministries/ pages.

The doc is fetched anonymously via the export endpoint —
https://docs.google.com/document/d/<id>/export?format=html — so no Google
credentials are involved; a doc that is not link-public redirects to the
Google sign-in host, which is detected and reported instead of cached.
"""

import re
from datetime import UTC, datetime

import httpx
import nh3
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Team, TeamPage

DOC_MAX_BYTES = 2_000_000
FETCH_TIMEOUT = 30.0
EXPORT_URL = "https://docs.google.com/document/d/{doc_id}/export?format=html"

_DOC_URL_RE = re.compile(
    r"^https://docs\.google\.com/document(?:/u/\d+)?/d/([A-Za-z0-9_-]+)"
)

# nh3's defaults (no script/iframe/event handlers/style tags), plus the class
# and style attributes everywhere: a Google Doc export carries ALL its
# formatting as classes referencing one <style> block, handled separately below
_ALLOWED_ATTRIBUTES = {**nh3.ALLOWED_ATTRIBUTES, "*": {"class", "style", "id"}}


def extract_doc_id(url: str) -> str:
    """The document id, or ValueError for anything but a Google Doc link."""
    match = _DOC_URL_RE.match(url.strip())
    if match is None:
        raise ValueError(
            "not a Google Doc link — expected https://docs.google.com/document/d/…"
        )
    return match.group(1)


async def set_home_doc_url(
    session: AsyncSession, team_id: int, url: str | None
) -> Team:
    """Set or clear the team's home page doc; validates the link shape."""
    team = await session.get(Team, team_id)
    if team is None:
        raise LookupError(f"team {team_id} not found")
    if url and url.strip():
        extract_doc_id(url)  # raises ValueError on junk
        team.home_doc_url = url.strip()
    else:
        team.home_doc_url = None
    await session.flush()
    return team


def slugify(path: str) -> str:
    """'Liturgy / Altar Servers' → 'liturgy-altar-servers'."""
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "team"


def slug_map(paths: dict[int, str]) -> dict[int, str]:
    """team id → URL slug from display paths. Path slugs are almost always
    unique (team names are unique per parent); a collision after slugification
    (e.g. 'A/B C' vs 'A/B-C') gets a -<id> suffix on every collider."""
    by_slug: dict[str, list[int]] = {}
    for team_id, path in paths.items():
        by_slug.setdefault(slugify(path), []).append(team_id)
    result: dict[int, str] = {}
    for slug, ids in by_slug.items():
        for team_id in ids:
            result[team_id] = slug if len(ids) == 1 else f"{slug}-{team_id}"
    return result


def sanitize_doc_html(raw: str) -> str:
    """Body of the exported doc through an allowlist sanitizer, with the
    <style> block from the head carried over (the export expresses all
    formatting as classes referencing it). The block is dropped whole if it
    contains '<' — CSS that could escape its tag has no business here."""
    style = ""
    style_match = re.search(r"<style[^>]*>(.*?)</style>", raw, re.S | re.I)
    if style_match and "<" not in style_match.group(1):
        style = f"<style>{style_match.group(1)}</style>\n"
    body_match = re.search(r"<body[^>]*>(.*)</body>", raw, re.S | re.I)
    body = body_match.group(1) if body_match else raw
    return style + nh3.clean(body, attributes=_ALLOWED_ATTRIBUTES)


async def fetch_and_store(
    session: AsyncSession, team: Team, client: httpx.AsyncClient
) -> TeamPage:
    """Fetch the team's doc and upsert its team_page row. Failures set
    status='error' but keep the previous good html (and its fetched_at), so a
    Google hiccup never blanks a published page."""
    page = await session.get(TeamPage, team.id)
    if page is None:
        page = TeamPage(team_id=team.id)
        session.add(page)
    try:
        doc_id = extract_doc_id(team.home_doc_url or "")
        response = await client.get(
            EXPORT_URL.format(doc_id=doc_id),
            follow_redirects=True,
            timeout=FETCH_TIMEOUT,
        )
        response.raise_for_status()
        if response.url.host != "docs.google.com":
            raise ValueError(
                "the doc is not publicly accessible "
                "(Google redirected to a sign-in page)"
            )
        if len(response.content) > DOC_MAX_BYTES:
            raise ValueError(f"doc HTML is over {DOC_MAX_BYTES // 1_000_000} MB")
        page.html = sanitize_doc_html(response.text)
        page.fetched_at = datetime.now(UTC)
        page.status = "ok"
        page.error = None
    except (httpx.HTTPError, ValueError) as exc:
        page.status = "error"
        page.error = str(exc)[:500]
    await session.flush()
    return page


async def published(session: AsyncSession) -> list[tuple[Team, TeamPage]]:
    """Active teams with a home doc and cached html — the public page set.
    An errored refetch still publishes its last good html."""
    rows = await session.execute(
        sa.select(Team, TeamPage)
        .join(TeamPage, TeamPage.team_id == Team.id)
        .where(
            Team.is_active, Team.home_doc_url.is_not(None), TeamPage.html.is_not(None)
        )
    )
    return [(team, page) for team, page in rows]
