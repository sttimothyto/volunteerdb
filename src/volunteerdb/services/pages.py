"""Team home pages: each team may point at one PUBLIC Google Doc, whose HTML
is fetched (nightly by jobs.fetch_pages, on demand from the team page),
sanitized, and cached in team_page for the no-login /ministries/ pages.

The doc is fetched anonymously via the export endpoint —
https://docs.google.com/document/d/<id>/export?format=html — so no Google
credentials are involved; a doc that is not link-public redirects to the
Google sign-in host, which is detected and reported instead of cached.

The export inlines every image as a base64 data: URI (older exports linked
googleusercontent URLs instead); _localize_images extracts both kinds into
team_page_image rows BEFORE sanitization — nh3 allows no data: scheme, so
an unextracted inline image would silently lose its src.
"""

import base64
import hashlib
import re
from datetime import datetime
from html import unescape
from io import BytesIO
from urllib.parse import urlsplit

import anyio.to_thread
import httpx
import nh3
import segno
import sqlalchemy as sa
import structlog
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import DomainError, External, Invalid, invalid, not_found, require
from ..fp import Err, Ok, Result
from ..models import PageStatus, Team, TeamPage, TeamPageImage
from ..permissions import Actor

logger = structlog.get_logger(__name__)

DOC_MAX_BYTES = 30_000_000  # raw export — mostly inline base64 image payload
HTML_MAX_BYTES = 2_000_000  # stored html, after the images are extracted out
FETCH_TIMEOUT = 30.0
EXPORT_URL = "https://docs.google.com/document/d/{doc_id}/export?format=html"

IMAGE_MAX_BYTES = 5_000_000  # per downloaded image
IMAGES_MAX_TOTAL = 20_000_000  # stored bytes per page
IMAGES_MAX_COUNT = 30
IMAGE_MAX_DIM = 1600  # longest side; bigger stills are downscaled
IMAGE_ROUTE = "/ministries/img/{team_id}/{seq}"
_IMAGE_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}

_DOC_URL_RE = re.compile(
    r"^https://docs\.google\.com/document(?:/u/\d+)?/d/([A-Za-z0-9_-]+)"
)
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=)(["\'])([^"\']*)\2', re.I | re.S)
_DATA_URI_RE = re.compile(r"data:image/[a-z0-9.+-]+;base64,(.+)", re.I | re.S)
_IMPORT_RE = re.compile(r"@import[^;]*;")
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")

# nh3's defaults (no script/iframe/event handlers/style tags), plus the class
# and style attributes everywhere: a Google Doc export carries ALL its
# formatting as classes referencing one <style> block, handled separately below
_ALLOWED_ATTRIBUTES = {**nh3.ALLOWED_ATTRIBUTES, "*": {"class", "style", "id"}}


def extract_doc_id(url: str) -> Result[str, Invalid]:
    """The document id, or Invalid for anything but a Google Doc link."""
    match = _DOC_URL_RE.match(url.strip())
    if match is None:
        return invalid(
            "not a Google Doc link — expected https://docs.google.com/document/d/…"
        )
    return Ok(match.group(1))


async def set_home_doc_url(
    session: AsyncSession, actor: Actor | None, team_id: int, url: str | None
) -> Result[Team, DomainError]:
    """Set or clear the team's home page doc; validates the link shape.

    Full-roster rights, so **core members included** — deliberately, and not an
    oversight to be tightened: ministry leaders here are often elderly, and a
    public page nobody can refresh goes stale. The content is nh3-sanitized and
    the URL must live on docs.google.com, so what is at stake is what the page
    says under a name the parish can correct."""
    if denied := require(
        actor is None or actor.can_view_full_roster(team_id),
        "manage this team's home page",
    ):
        return denied
    team = await session.get(Team, team_id)
    if team is None:
        return not_found("team", team_id)
    if url and url.strip():
        parsed = extract_doc_id(url)
        if isinstance(parsed, Err):
            return parsed
        team.home_doc_url = url.strip()
    else:
        team.home_doc_url = None
    await session.flush()
    return Ok(team)


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


def _scope_css(css: str) -> str:
    """Every selector prefixed with `.doc ` (bare body/html become `.doc`), so
    the doc's element rules (Google exports `body{}`, `p{}`, `h1{}`…) cannot
    restyle the themed shell around it. @import is stripped; any other @-rule
    drops the block whole — same spirit as the '<' guard in the caller."""
    css = _IMPORT_RE.sub("", css)
    if "@" in css:
        return ""
    rules = []
    for match in _CSS_RULE_RE.finditer(css):
        scoped = []
        for selector in match.group(1).split(","):
            selector = selector.strip()
            if not selector:
                continue
            if selector in ("body", "html"):
                scoped.append(".doc")
            elif selector.startswith(("body ", "html ")):
                scoped.append(".doc " + selector.split(" ", 1)[1])
            else:
                scoped.append(".doc " + selector)
        if scoped:
            rules.append(f"{','.join(scoped)}{{{match.group(2)}}}")
    return "".join(rules)


def sanitize_doc_html(raw: str) -> str:
    """Body of the exported doc through an allowlist sanitizer, with the
    <style> block from the head carried over (the export expresses all
    formatting as classes referencing it) scoped under .doc. The block is
    dropped whole if it contains '<' — CSS that could escape its tag has no
    business here."""
    style = ""
    style_match = re.search(r"<style[^>]*>(.*?)</style>", raw, re.S | re.I)
    if style_match and "<" not in style_match.group(1):
        scoped = _scope_css(style_match.group(1))
        if scoped:
            style = f"<style>{scoped}</style>\n"
    body_match = re.search(r"<body[^>]*>(.*)</body>", raw, re.S | re.I)
    body = body_match.group(1) if body_match else raw
    return style + nh3.clean(body, attributes=_ALLOWED_ATTRIBUTES)


def _prepare_image(data: bytes) -> Result[tuple[bytes, str], Invalid]:
    """Sync and CPU-bound — call via anyio.to_thread.run_sync. Doubles as the
    content guard on fetched bytes: whatever Google returned must decode as an
    image or it is not stored. Oversized stills are downscaled to
    IMAGE_MAX_DIM; everything else is stored byte-identical."""
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ):
        return invalid("not a readable image")
    if img.format not in _IMAGE_FORMATS:
        return invalid(f"unsupported image format: {img.format}")
    if getattr(img, "is_animated", False) or max(img.size) <= IMAGE_MAX_DIM:
        return Ok((data, Image.MIME[img.format]))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    if img.mode in ("RGBA", "LA", "P"):  # may carry transparency
        img.save(buffer, format="PNG", optimize=True)
        return Ok((buffer.getvalue(), "image/png"))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(buffer, format="JPEG", quality=85, optimize=True)
    return Ok((buffer.getvalue(), "image/jpeg"))


def _is_google_image(url: str) -> bool:
    """SSRF guard: only Google's image CDN is ever fetched."""
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(
        ".googleusercontent.com"
    )


async def _one_image(
    url: str, data_uri: re.Match[str] | None, client: httpx.AsyncClient
) -> Result[tuple[bytes, str], Invalid | External]:
    """One embedded image as stored bytes and a content type, or why not."""
    if data_uri is not None:
        try:  # b64decode raises a ValueError subclass
            raw = base64.b64decode(re.sub(r"\s+", "", data_uri.group(1)), validate=True)
        except ValueError as exc:
            return invalid(str(exc))
    else:
        try:
            response = await client.get(
                url, follow_redirects=True, timeout=FETCH_TIMEOUT
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return Err(External("google docs", str(exc)))
        if not _is_google_image(str(response.url)):
            return invalid("image redirected outside googleusercontent.com")
        raw = response.content
    if len(raw) > IMAGE_MAX_BYTES:
        return invalid(f"image over {IMAGE_MAX_BYTES // 1_000_000} MB")
    return await anyio.to_thread.run_sync(_prepare_image, raw)


async def _localize_images(
    html: str, team_id: int, client: httpx.AsyncClient
) -> tuple[str, list[TeamPageImage]]:
    """Extract the doc's images — inline base64 data: URIs (what the export
    emits today) and googleusercontent-hosted URLs (older exports; their
    signed URLs expire, so hotlinks rot) — into team_page_image rows and
    rewrite their srcs to IMAGE_ROUTE plus a ?v=<content hash> — the route
    serves a hashed URL immutable-cacheable, and equal output html thereby
    guarantees equal stored image bytes (which fetch_and_store relies on to
    skip rewriting unchanged image rows). A failed image keeps its original
    src, so one broken image never unpublishes a page (a failed data: src is
    then dropped by the sanitizer, which allows no data: scheme, leaving an
    img with no src). Other srcs are left alone and never fetched."""
    seq_by_url: dict[str, tuple[int, str]] = {}
    failed: set[str] = set()
    images: list[TeamPageImage] = []
    total = 0
    for match in _IMG_SRC_RE.finditer(html):
        url = unescape(match.group(3))
        if url in seq_by_url or url in failed:
            continue
        data_uri = _DATA_URI_RE.match(url)
        if data_uri is None and not _is_google_image(url):
            continue
        if len(images) >= IMAGES_MAX_COUNT or total >= IMAGES_MAX_TOTAL:
            logger.info("page image budget exhausted", team_id=team_id)
            break
        prepared = await _one_image(url, data_uri, client)
        if isinstance(prepared, Err):
            failed.add(url)
            logger.info(
                "page image not extracted",
                team_id=team_id,
                src=url[:100],
                error=prepared.error.message,
            )
            continue
        data, content_type = prepared.value
        seq_by_url[url] = (len(images) + 1, hashlib.sha256(data).hexdigest()[:12])
        images.append(
            TeamPageImage(
                team_id=team_id,
                seq=len(images) + 1,
                image=data,
                content_type=content_type,
            )
        )
        total += len(data)

    def replace(match: re.Match[str]) -> str:
        entry = seq_by_url.get(unescape(match.group(3)))
        if entry is None:
            return match.group(0)
        seq, digest = entry
        local = IMAGE_ROUTE.format(team_id=team_id, seq=seq)
        return f'{match.group(1)}"{local}?v={digest}"'

    return _IMG_SRC_RE.sub(replace, html), images


async def fetch_and_store(
    session: AsyncSession,
    team: Team,
    client: httpx.AsyncClient,
    force: bool = False,
    *,
    actor: Actor | None = None,
    now: datetime,
) -> Result[TeamPage, DomainError]:
    """Fetch the team's doc and upsert its team_page row, downloading embedded
    images into team_page_image (replaced wholesale). `actor` is keyword-only
    and defaults to None because the nightly job is the ordinary caller; pass
    one where a person asked for the refetch, and it takes the same rights as
    setting the URL.

    Failures set status='error' but keep the previous good html, images and
    fetched_at, so a Google hiccup never blanks a published page.

    An unchanged doc skips the image delete+reinsert: the ?v= content hashes
    _localize_images bakes into the html mean equal html entails identical
    image bytes, so rewriting up to 20 MB of BYTEA nightly per page would be
    pure WAL/vacuum churn. force=True rewrites regardless — the manual
    "Fetch now" button, and the repair path for image rows damaged
    out-of-band."""
    if denied := require(
        actor is None or actor.can_view_full_roster(team.id),
        "refresh this team's home page",
    ):
        return denied
    page = await session.get(TeamPage, team.id)
    if page is None:
        page = TeamPage(team_id=team.id)
        session.add(page)
    fetched = await _fetch_doc(team, client)
    if isinstance(fetched, Err):
        page.status = PageStatus.error
        page.error = fetched.error.message[:500]
        await session.flush()
        return Ok(page)
    html, images = fetched.value
    if force or page.status != PageStatus.ok or page.html != html:
        await session.execute(
            sa.delete(TeamPageImage).where(TeamPageImage.team_id == team.id)
        )
        session.add_all(images)
        page.html = html
    else:
        logger.info("page unchanged; image rows kept", team_id=team.id)
    page.fetched_at = now
    page.status = PageStatus.ok
    page.error = None
    await session.flush()
    return Ok(page)


async def _fetch_doc(
    team: Team, client: httpx.AsyncClient
) -> Result[tuple[str, list[TeamPageImage]], Invalid | External]:
    """The doc's sanitized html and its extracted images, or why the fetch
    could not be used -- which the caller records on the page row rather
    than refusing anything."""
    doc_id = extract_doc_id(team.home_doc_url or "")
    if isinstance(doc_id, Err):
        return doc_id
    try:
        response = await client.get(
            EXPORT_URL.format(doc_id=doc_id.value),
            follow_redirects=True,
            timeout=FETCH_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return Err(External("google docs", str(exc)))
    if response.url.host != "docs.google.com":
        return invalid(
            "the doc is not publicly accessible (Google redirected to a sign-in page)"
        )
    if len(response.content) > DOC_MAX_BYTES:
        return invalid(f"doc HTML is over {DOC_MAX_BYTES // 1_000_000} MB")
    # images first: the sanitizer would strip their data: srcs
    html, images = await _localize_images(response.text, team.id, client)
    html = sanitize_doc_html(html)
    if len(html.encode()) > HTML_MAX_BYTES:
        return invalid(
            f"doc text is over {HTML_MAX_BYTES // 1_000_000} MB with its images extracted"
        )
    return Ok((html, images))


def qr_png(url: str) -> bytes:
    """PNG QR code for `url`, ~1000px including quiet zone — print-usable."""
    qr = segno.make(url, error="m", micro=False)
    width, _ = qr.symbol_size(scale=1, border=4)
    buffer = BytesIO()
    qr.save(buffer, kind="png", scale=max(1, round(1000 / width)), border=4)
    return buffer.getvalue()


async def page_status(
    session: AsyncSession, actor: Actor | None, team_id: int
) -> Result[TeamPage | None, DomainError]:
    """The cached page's publishing state, or None when the team has no doc set.

    Same audience as setting the URL — core members included — because the
    people who keep the page current are the people who need to know whether
    the last fetch worked. The HTML itself is not the point here: that is served
    to the world at /ministries/<slug>.html."""
    if denied := require(
        actor is None or actor.can_view_full_roster(team_id),
        "see this team's page status",
    ):
        return denied
    return Ok(await session.get(TeamPage, team_id))


async def published_teams(session: AsyncSession) -> list[Team]:
    """Active teams with a home doc and cached html — the public page set.
    An errored refetch still publishes its last good html. Team rows only:
    the html (up to 2 MB per page) stays in the database."""
    rows = await session.scalars(
        sa.select(Team)
        .join(TeamPage, TeamPage.team_id == Team.id)
        .where(
            Team.is_active, Team.home_doc_url.is_not(None), TeamPage.html.is_not(None)
        )
    )
    return list(rows)


async def is_published(session: AsyncSession, team_id: int) -> bool:
    """Whether this team has a live public page — the same predicate as
    published_page, without loading the html it exists to serve. The team page
    draws a link off this for every reader, and a page can run to 2 MB.

    No permission gate: /ministries/<slug>.html is world-readable, so whether
    one exists is public too."""
    return (
        await session.scalar(
            sa.select(TeamPage.team_id)
            .join(Team, Team.id == TeamPage.team_id)
            .where(
                TeamPage.team_id == team_id,
                Team.is_active,
                Team.home_doc_url.is_not(None),
                TeamPage.html.is_not(None),
            )
        )
    ) is not None


async def published_page(session: AsyncSession, team_id: int) -> TeamPage | None:
    """The one published page for team_id, or None — same predicate as
    published_teams, so an unpublished/inactive team reads as absent."""
    return (
        await session.execute(
            sa.select(TeamPage)
            .join(Team, Team.id == TeamPage.team_id)
            .where(
                TeamPage.team_id == team_id,
                Team.is_active,
                Team.home_doc_url.is_not(None),
                TeamPage.html.is_not(None),
            )
        )
    ).scalar_one_or_none()
