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

from ..models import Team, TeamPage, TeamPageImage

logger = structlog.get_logger(__name__)

DOC_MAX_BYTES = 2_000_000
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
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]*)(")', re.I | re.S)
_IMPORT_RE = re.compile(r"@import[^;]*;")
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")

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


def _prepare_image(data: bytes) -> tuple[bytes, str]:
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
    ) as exc:
        raise ValueError("not a readable image") from exc
    if img.format not in _IMAGE_FORMATS:
        raise ValueError(f"unsupported image format: {img.format}")
    if getattr(img, "is_animated", False) or max(img.size) <= IMAGE_MAX_DIM:
        return data, Image.MIME[img.format]
    img = ImageOps.exif_transpose(img)
    img.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    if img.mode in ("RGBA", "LA", "P"):  # may carry transparency
        img.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "image/png"
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(buffer, format="JPEG", quality=85, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def _is_google_image(url: str) -> bool:
    """SSRF guard: only Google's image CDN is ever fetched."""
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(
        ".googleusercontent.com"
    )


async def _localize_images(
    html: str, team_id: int, client: httpx.AsyncClient
) -> tuple[str, list[TeamPageImage]]:
    """Download the doc's googleusercontent-hosted images (the export's signed
    URLs expire, so hotlinks rot) and rewrite their srcs to IMAGE_ROUTE. A
    failed image keeps its remote src — one broken image never unpublishes a
    page. Non-Google srcs are left alone and never fetched."""
    seq_by_url: dict[str, int] = {}
    failed: set[str] = set()
    images: list[TeamPageImage] = []
    total = 0
    for match in _IMG_SRC_RE.finditer(html):
        url = unescape(match.group(2))
        if url in seq_by_url or url in failed or not _is_google_image(url):
            continue
        if len(images) >= IMAGES_MAX_COUNT or total >= IMAGES_MAX_TOTAL:
            logger.info("page image budget exhausted", team_id=team_id)
            break
        try:
            response = await client.get(
                url, follow_redirects=True, timeout=FETCH_TIMEOUT
            )
            response.raise_for_status()
            if not _is_google_image(str(response.url)):
                raise ValueError("image redirected outside googleusercontent.com")
            if len(response.content) > IMAGE_MAX_BYTES:
                raise ValueError(f"image over {IMAGE_MAX_BYTES // 1_000_000} MB")
            data, content_type = await anyio.to_thread.run_sync(
                _prepare_image, response.content
            )
        except (httpx.HTTPError, ValueError) as exc:
            failed.add(url)
            logger.info(
                "page image kept remote", team_id=team_id, url=url, error=str(exc)
            )
            continue
        seq_by_url[url] = len(images) + 1
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
        seq = seq_by_url.get(unescape(match.group(2)))
        if seq is None:
            return match.group(0)
        local = IMAGE_ROUTE.format(team_id=team_id, seq=seq)
        return match.group(1) + local + match.group(3)

    return _IMG_SRC_RE.sub(replace, html), images


async def fetch_and_store(
    session: AsyncSession, team: Team, client: httpx.AsyncClient
) -> TeamPage:
    """Fetch the team's doc and upsert its team_page row, downloading embedded
    images into team_page_image (replaced wholesale). Failures set
    status='error' but keep the previous good html, images and fetched_at, so
    a Google hiccup never blanks a published page."""
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
        html = sanitize_doc_html(response.text)
        html, images = await _localize_images(html, team.id, client)
        await session.execute(
            sa.delete(TeamPageImage).where(TeamPageImage.team_id == team.id)
        )
        session.add_all(images)
        page.html = html
        page.fetched_at = datetime.now(UTC)
        page.status = "ok"
        page.error = None
    except (httpx.HTTPError, ValueError) as exc:
        page.status = "error"
        page.error = str(exc)[:500]
    await session.flush()
    return page


def qr_png(url: str) -> bytes:
    """PNG QR code for `url`, ~1000px including quiet zone — print-usable."""
    qr = segno.make(url, error="m", micro=False)
    width, _ = qr.symbol_size(scale=1, border=4)
    buffer = BytesIO()
    qr.save(buffer, kind="png", scale=max(1, round(1000 / width)), border=4)
    return buffer.getvalue()


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
