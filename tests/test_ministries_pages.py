"""Public ministry pages: doc-link validation, sanitization, fetch, routes.

The /ministries/ pages are the app's only anonymous surface beyond /login —
they must serve without a session, show only published active teams, and
never serve unsanitized doc HTML.
"""

import base64
import hashlib
from io import BytesIO

import httpx
import pytest
import sqlalchemy as sa
from PIL import Image

from volunteerdb.db import db_session
from volunteerdb.models import TeamPage, TeamPageImage
from volunteerdb.services import pages, teams

from tests.fp_helpers import ok

DOC_HTML = (
    "<html><head><meta content='text/html'>"
    "<style type='text/css'>.c1{font-weight:700}</style></head>"
    "<body class='doc'><p class='c1' onclick='alert(1)'>Welcome volunteers</p>"
    "<script>evil()</script><iframe src='https://evil.test'></iframe></body></html>"
)

IMG_URL = "https://lh7-rt.googleusercontent.com/docsz/AAA?key=sig1&x=2"
IMG_DOC_HTML = (
    "<html><head></head><body>"
    "<img src='https://lh7-rt.googleusercontent.com/docsz/AAA?key=sig1&amp;x=2'"
    " alt='choir' width='624'>"
    "<img src='https://evil.test/x.png'><img src='images/local.png'>"
    "</body></html>"
)


def _png(size=(40, 30), mode="RGB", color="red") -> bytes:
    buffer = BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _v(image: bytes) -> str:
    """The ?v= content hash _localize_images bakes into localized srcs."""
    return hashlib.sha256(image).hexdigest()[:12]


# --- units -----------------------------------------------------------------


def test_extract_doc_id():
    doc_id = pages.extract_doc_id(
        "https://docs.google.com/document/d/1AbC-xyz_9/edit?usp=sharing"
    )
    assert doc_id == "1AbC-xyz_9"
    assert pages.extract_doc_id("https://docs.google.com/document/u/0/d/QQ22") == "QQ22"

    for bad in (
        "https://example.com/document/d/123",
        "https://docs.google.com/spreadsheets/d/123",
        "http://docs.google.com/document/d/123",  # https only
        "not a url",
        "",
    ):
        with pytest.raises(ValueError):
            pages.extract_doc_id(bad)


def test_slugify_and_collisions():
    assert pages.slugify("Liturgy / Altar Servers") == "liturgy-altar-servers"
    assert pages.slugify("  Café & Crêpes!  ") == "caf-cr-pes"
    assert pages.slugify("///") == "team", "never an empty slug"

    slugs = pages.slug_map({1: "Liturgy / Music", 2: "Liturgy Music", 3: "Youth"})
    assert slugs[3] == "youth"
    assert slugs[1] == "liturgy-music-1" and slugs[2] == "liturgy-music-2", (
        "colliding slugs get an id suffix on every collider"
    )


def test_sanitize_doc_html_strips_active_content():
    clean = pages.sanitize_doc_html(DOC_HTML)
    assert "<style>.doc .c1{font-weight:700}</style>" in clean, (
        "head styles carried over, scoped under .doc"
    )
    assert 'class="c1"' in clean and "Welcome volunteers" in clean
    assert "script" not in clean and "evil()" not in clean
    assert "iframe" not in clean
    assert "onclick" not in clean


def test_scope_css_confines_doc_styles_to_the_doc_card():
    """Google exports element rules (body{}, p{}, h1{}…) that would otherwise
    restyle the themed shell around the doc."""
    assert pages._scope_css("body{background:#fff}") == ".doc{background:#fff}"
    assert pages._scope_css("p,h1{margin:0}") == ".doc p,.doc h1{margin:0}"
    assert pages._scope_css("body p{margin:0}") == ".doc p{margin:0}"
    assert (
        pages._scope_css("@import url(https://evil.test/x.css);.c1{a:b}")
        == ".doc .c1{a:b}"
    ), "@import is stripped, the rest survives"
    assert pages._scope_css("@media print{.c1{a:b}}") == "", (
        "any other @-rule drops the block whole"
    )


def test_qr_png_is_a_print_usable_png():
    png = pages.qr_png("https://vdb.example.org/ministries/choir.html")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = Image.open(BytesIO(png)).size
    assert width == height >= 800, "print-usable, not a thumbnail"


def test_sanitize_drops_style_block_containing_markup():
    """CSS with a '<' could smuggle markup past the tag (e.g. via content:
    strings); the whole block is dropped rather than risk it."""
    raw = (
        '<head><style>.a{content:"<img src=x onerror=boom()>"}</style></head>'
        "<body><p>x</p></body>"
    )
    clean = pages.sanitize_doc_html(raw)
    assert "<style>" not in clean and "onerror" not in clean
    assert "<p>x</p>" in clean


# --- set_home_doc_url ------------------------------------------------------


async def test_set_home_doc_url_validates_and_clears(database):
    async with db_session() as session:
        team = ok(await teams.create(session, None, "Choir"))
        await pages.set_home_doc_url(
            session, None, team.id, "https://docs.google.com/document/d/abc123/edit"
        )
        assert team.home_doc_url == "https://docs.google.com/document/d/abc123/edit"

        with pytest.raises(ValueError):
            await pages.set_home_doc_url(
                session, None, team.id, "https://evil.test/doc"
            )

        await pages.set_home_doc_url(session, None, team.id, None)
        assert team.home_doc_url is None

        with pytest.raises(LookupError):
            await pages.set_home_doc_url(session, None, 99999, None)


# --- fetch_and_store -------------------------------------------------------


def _doc_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _team_with_doc(name="Choir", doc_id="abc123"):
    async with db_session() as session:
        team = ok(await teams.create(session, None, name))
        await pages.set_home_doc_url(
            session, None, team.id, f"https://docs.google.com/document/d/{doc_id}/edit"
        )
        return team.id


async def test_fetch_and_store_success(database):
    team_id = await _team_with_doc()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/document/d/abc123/export"
        return httpx.Response(200, text=DOC_HTML)

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "ok" and page.error is None
        assert page.fetched_at is not None
        assert "Welcome volunteers" in page.html and "script" not in page.html


async def test_fetch_failure_keeps_last_good_html(database):
    team_id = await _team_with_doc()

    async with (
        db_session() as session,
        _doc_client(lambda request: httpx.Response(200, text=DOC_HTML)) as client,
    ):
        team = await teams.get(session, team_id)
        await pages.fetch_and_store(session, team, client)

    async with (
        db_session() as session,
        _doc_client(lambda request: httpx.Response(404, text="gone")) as client,
    ):
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "error" and "404" in page.error
        assert "Welcome volunteers" in page.html, "a hiccup never blanks the page"


async def test_fetch_detects_private_doc_signin_redirect(database):
    team_id = await _team_with_doc()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "docs.google.com":
            return httpx.Response(
                302, headers={"location": "https://accounts.google.com/signin"}
            )
        return httpx.Response(200, text="<html>Sign in</html>")

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "error"
        assert "not publicly accessible" in page.error
        assert page.html is None, "the sign-in page must never be cached"


async def test_fetch_rejects_oversized_doc(database):
    team_id = await _team_with_doc()

    async with (
        db_session() as session,
        _doc_client(
            lambda request: httpx.Response(200, text="x" * (pages.DOC_MAX_BYTES + 1))
        ) as client,
    ):
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "error" and "over 30 MB" in page.error


async def test_fetch_rejects_oversized_text_after_extraction(database):
    """DOC_MAX_BYTES admits image-heavy exports; the stored html — images
    extracted out — keeps the old 2 MB cap."""
    team_id = await _team_with_doc()
    doc = "<body><p>" + "x" * (pages.HTML_MAX_BYTES + 1) + "</p></body>"

    async with (
        db_session() as session,
        _doc_client(lambda request: httpx.Response(200, text=doc)) as client,
    ):
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "error" and "over 2 MB" in page.error


# --- image localization ----------------------------------------------------


def _doc_and_image_handler(doc_html: str, image: httpx.Response):
    """MockTransport handler serving the doc from docs.google.com and `image`
    from googleusercontent; anything else is a test failure (SSRF guard)."""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "docs.google.com":
            return httpx.Response(200, text=doc_html)
        if request.url.host.endswith(".googleusercontent.com"):
            return image
        raise AssertionError(f"fetched a non-Google host: {request.url}")

    return handler, hosts


async def _images(session, team_id: int) -> list[TeamPageImage]:
    rows = await session.execute(
        sa.select(TeamPageImage)
        .where(TeamPageImage.team_id == team_id)
        .order_by(TeamPageImage.seq)
    )
    return list(rows.scalars())


async def test_fetch_localizes_google_images(database):
    """The export's signed googleusercontent URLs expire, so the bytes are
    stored locally and the src rewritten; other srcs are left alone and —
    critically — never fetched."""
    team_id = await _team_with_doc()
    png = _png()
    handler, hosts = _doc_and_image_handler(
        IMG_DOC_HTML, httpx.Response(200, content=png)
    )

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "ok", page.error
        assert f'src="/ministries/img/{team_id}/1?v={_v(png)}"' in page.html
        assert 'alt="choir"' in page.html and 'width="624"' in page.html
        assert "googleusercontent" not in page.html
        assert 'src="https://evil.test/x.png"' in page.html, "non-Google src kept"
        assert 'src="images/local.png"' in page.html, "relative src kept"
        assert "evil.test" not in hosts, "no non-Google host is ever fetched"

        (row,) = await _images(session, team_id)
        assert row.seq == 1 and row.image == png and row.content_type == "image/png"


async def test_fetch_localizes_inline_data_uri_images(database):
    """Today's exports inline every image as a base64 data: URI. The bytes are
    extracted into team_page_image and the src rewritten — the sanitizer
    (which allows no data: scheme) would otherwise strip the src entirely,
    which is exactly how the young-adults logo went missing."""
    team_id = await _team_with_doc()
    png = _png()
    encoded = base64.b64encode(png).decode()
    doc = (
        "<html><head></head><body>"
        f'<img alt="logo" src="data:image/png;base64,{encoded}">'
        "<p>hello</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "docs.google.com", "data URIs need no fetch"
        return httpx.Response(200, text=doc)

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "ok", page.error
        assert f'src="/ministries/img/{team_id}/1?v={_v(png)}"' in page.html
        assert 'alt="logo"' in page.html
        assert "data:" not in page.html

        (row,) = await _images(session, team_id)
        assert row.seq == 1 and row.image == png and row.content_type == "image/png"


@pytest.mark.parametrize(
    "payload",
    ["AAAA", "!!not-base64!!", base64.b64encode(b"x" * 100).decode()],
    ids=["not-an-image", "invalid-base64", "garbage-bytes"],
)
async def test_unreadable_data_uri_loses_its_src_but_page_publishes(database, payload):
    """A data: src that fails extraction can't be kept like a remote src — the
    sanitizer strips the scheme — so the img just loses its src; the page
    itself still publishes."""
    team_id = await _team_with_doc()
    doc = (
        f'<body><img src="data:image/png;base64,{payload}" alt="broken">'
        "<p>hello</p></body>"
    )

    async with (
        db_session() as session,
        _doc_client(lambda request: httpx.Response(200, text=doc)) as client,
    ):
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "ok"
        assert "data:" not in page.html
        assert 'alt="broken"' in page.html and "<p>hello</p>" in page.html
        assert await _images(session, team_id) == []


async def test_duplicate_image_src_downloads_once(database):
    team_id = await _team_with_doc()
    doc = f"<body><img src='{IMG_URL}'><p>x</p><img src='{IMG_URL}'></body>"
    handler, hosts = _doc_and_image_handler(doc, httpx.Response(200, content=_png()))

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.html.count(f'src="/ministries/img/{team_id}/1?v=') == 2
        assert len(await _images(session, team_id)) == 1
        assert hosts.count("lh7-rt.googleusercontent.com") == 1


@pytest.mark.parametrize(
    "image_response",
    [
        httpx.Response(404, text="gone"),
        httpx.Response(200, content=b"not an image"),
        httpx.Response(200, content=b"x" * (pages.IMAGE_MAX_BYTES + 1)),
    ],
    ids=["http-error", "not-an-image", "oversized"],
)
async def test_failed_image_keeps_remote_src_and_page_ok(database, image_response):
    """One broken image never unpublishes a page."""
    team_id = await _team_with_doc()
    handler, _ = _doc_and_image_handler(IMG_DOC_HTML, image_response)

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "ok"
        assert "lh7-rt.googleusercontent.com" in page.html, "remote src kept"
        assert await _images(session, team_id) == []


async def test_oversized_still_is_downscaled(database):
    team_id = await _team_with_doc()
    handler, _ = _doc_and_image_handler(
        IMG_DOC_HTML, httpx.Response(200, content=_png(size=(3000, 2000)))
    )

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        await pages.fetch_and_store(session, team, client)
        (row,) = await _images(session, team_id)
        assert max(Image.open(BytesIO(row.image)).size) <= pages.IMAGE_MAX_DIM


async def test_refetch_replaces_images_but_failure_keeps_them(database):
    team_id = await _team_with_doc()
    first = _png(color="red")
    handler, _ = _doc_and_image_handler(
        IMG_DOC_HTML, httpx.Response(200, content=first)
    )
    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        await pages.fetch_and_store(session, team, client)

    # refetch with a new image: replaced wholesale
    second = _png(color="blue")
    handler, _ = _doc_and_image_handler(
        IMG_DOC_HTML, httpx.Response(200, content=second)
    )
    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        await pages.fetch_and_store(session, team, client)
        (row,) = await _images(session, team_id)
        assert row.image == second

    # doc fetch failure: old html AND old images survive together
    async with (
        db_session() as session,
        _doc_client(lambda request: httpx.Response(404, text="gone")) as client,
    ):
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "error"
        (row,) = await _images(session, team_id)
        assert row.image == second


async def test_refetch_with_unchanged_doc_keeps_image_rows(database, log_records):
    """An unchanged doc must not delete+reinsert the image rows — the nightly
    job would otherwise rewrite every page's BYTEA every night for nothing."""
    team_id = await _team_with_doc()
    png = _png()
    handler, _ = _doc_and_image_handler(IMG_DOC_HTML, httpx.Response(200, content=png))

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        first = await pages.fetch_and_store(session, team, client)
        first_html, first_fetched = first.html, first.fetched_at

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "ok" and page.html == first_html
        assert page.fetched_at > first_fetched, "the freshness label still advances"
        (row,) = await _images(session, team_id)
        assert row.image == png
    assert any(e["event"] == "page unchanged; image rows kept" for e in log_records)


async def test_force_refetch_takes_the_rewrite_path(database, log_records):
    """force=True (the manual "Fetch now" button) rewrites even an unchanged
    page — the repair path for image rows damaged out-of-band."""
    team_id = await _team_with_doc()
    handler, _ = _doc_and_image_handler(
        IMG_DOC_HTML, httpx.Response(200, content=_png())
    )

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        await pages.fetch_and_store(session, team, client)

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client, force=True)
        assert page.status == "ok"
        assert len(await _images(session, team_id)) == 1
    assert not any(e["event"] == "page unchanged; image rows kept" for e in log_records)


async def test_error_then_identical_doc_rewrites_and_clears_error(database):
    """status='error' forces the full path even when the refetched html equals
    the kept copy: error→ok always repairs the image set and clears error."""
    team_id = await _team_with_doc()
    png = _png()
    handler, _ = _doc_and_image_handler(IMG_DOC_HTML, httpx.Response(200, content=png))

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        await pages.fetch_and_store(session, team, client)

    async with (
        db_session() as session,
        _doc_client(lambda request: httpx.Response(404, text="gone")) as client,
    ):
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "error"

    async with db_session() as session, _doc_client(handler) as client:
        team = await teams.get(session, team_id)
        page = await pages.fetch_and_store(session, team, client)
        assert page.status == "ok" and page.error is None
        (row,) = await _images(session, team_id)
        assert row.image == png


# --- the public routes -----------------------------------------------------


async def _publish(name: str, html: str = "<p>hello</p>", active=True) -> int:
    async with db_session() as session:
        team = ok(await teams.create(session, None, name))
        await pages.set_home_doc_url(
            session,
            None,
            team.id,
            "https://docs.google.com/document/d/x" + str(team.id),
        )
        session.add(TeamPage(team_id=team.id, html=html, status="ok"))
        if not active:
            ok(await teams.update(session, None, team.id, is_active=False))
        return team.id


async def test_public_index_lists_only_published_active_teams(real_app_client):
    await _publish("Choir")
    await _publish("Closed Ministry", active=False)
    async with db_session() as session:
        ok(await teams.create(session, None, "No Page Team"))

    r = await real_app_client.get("/ministries/", follow_redirects=False)
    assert r.status_code == 200, "anonymous — no login redirect"
    body = r.text
    assert "Choir" in body and "/ministries/choir.html" in body
    assert "Closed Ministry" not in body, "inactive teams are unpublished"
    assert "No Page Team" not in body
    assert "/static/theme.css" in body, "public pages wear the app theme"
    assert '<a class="signin" href="/login">Sign in</a>' in body, (
        "the way in, offered to every reader alike: the page is cached once for "
        "the whole crowd, so a reader who already holds a session is sent on by "
        "/login itself (ui/login.py)"
    )


async def test_public_team_page_serves_cached_html(real_app_client):
    await _publish("Choir", html="<p class='c1'>Practice is Thursday</p>")

    r = await real_app_client.get("/ministries/choir.html", follow_redirects=False)
    assert r.status_code == 200
    assert "Practice is Thursday" in r.text
    assert "Choir" in r.text
    assert r.headers["cache-control"] == "public, max-age=300"
    assert "/static/theme.css" in r.text and "vdb-page-title" in r.text, (
        "public pages wear the app theme"
    )
    assert '<p class="back"><a href="/ministries/">' in r.text, (
        "a reader who arrived by QR code or shared link can reach the index"
    )
    assert "#FAF6EF" not in r.text, "the old hand-rolled palette is gone"

    r = await real_app_client.get("/ministries/no-such-team.html")
    assert r.status_code == 404

    r = await real_app_client.get("/ministries", follow_redirects=False)
    assert r.status_code in (302, 307) and "/ministries/" in r.headers["location"]


async def test_unpublished_team_page_404s(real_app_client):
    async with db_session() as session:
        ok(await teams.create(session, None, "Quiet Team"))
    r = await real_app_client.get("/ministries/quiet-team.html")
    assert r.status_code == 404


async def test_published_page_and_teams_share_the_predicate(database):
    """published_page(team_id) and is_published(team_id) must read as absent
    for exactly the teams published_teams excludes — inactive, doc-less, or
    without cached html."""
    published_id = await _publish("Choir")
    inactive_id = await _publish("Closed Ministry", active=False)
    async with db_session() as session:
        no_html = ok(await teams.create(session, None, "Pending Fetch"))
        await pages.set_home_doc_url(
            session, None, no_html.id, "https://docs.google.com/document/d/p1"
        )
        session.add(TeamPage(team_id=no_html.id, html=None, status="error"))
        no_doc = ok(await teams.create(session, None, "Unlinked"))
        session.add(TeamPage(team_id=no_doc.id, html="<p>orphan</p>", status="ok"))
        no_html_id, no_doc_id = no_html.id, no_doc.id

    async with db_session() as session:
        assert [t.id for t in await pages.published_teams(session)] == [published_id]
        page = await pages.published_page(session, published_id)
        assert page is not None and page.html == "<p>hello</p>"
        # the team page asks this one for every reader, so it must not drift
        assert await pages.is_published(session, published_id) is True
        for absent_id in (inactive_id, no_html_id, no_doc_id, 99999):
            assert await pages.published_page(session, absent_id) is None
            assert await pages.is_published(session, absent_id) is False


async def test_ministry_image_route_serves_published_teams_only(real_app_client):
    team_id = await _publish("Choir")
    png = _png()
    async with db_session() as session:
        session.add(
            TeamPageImage(team_id=team_id, seq=1, image=png, content_type="image/png")
        )

    r = await real_app_client.get(f"/ministries/img/{team_id}/1")
    assert r.status_code == 200, "anonymous — no login redirect"
    assert r.content == png
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=300", (
        "a bare URL (pre-hashing html) keeps the short lifetime"
    )

    r = await real_app_client.get(f"/ministries/img/{team_id}/1?v={_v(png)}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable", (
        "a hashed URL names exact bytes and is cached like a volunteer photo"
    )

    r = await real_app_client.get(f"/ministries/img/{team_id}/2")
    assert r.status_code == 404, "unknown seq"

    async with db_session() as session:
        ok(await teams.update(session, None, team_id, is_active=False))
    r = await real_app_client.get(f"/ministries/img/{team_id}/1")
    assert r.status_code == 404, "deactivating a team takes its images offline"

    async with db_session() as session:
        ok(await teams.update(session, None, team_id, is_active=True))
        await pages.set_home_doc_url(session, None, team_id, None)
    r = await real_app_client.get(f"/ministries/img/{team_id}/1")
    assert r.status_code == 404, "unlinking the doc takes its images offline"


# --- the API surface -------------------------------------------------------


async def test_home_doc_patch_permissions(client, seeded, token_leader, token_member):
    team_id = seeded["team_id"]
    url = "https://docs.google.com/document/d/abc123/edit"

    r = await client.patch(
        f"/api/teams/{team_id}/home-doc", json={"url": url}, headers=token_member
    )
    assert r.status_code == 403, "a plain member may not set the home page"

    r = await client.patch(
        f"/api/teams/{team_id}/home-doc", json={"url": url}, headers=token_leader
    )
    assert r.status_code == 200, r.text
    assert r.json()["home_doc_url"] == url

    r = await client.patch(
        f"/api/teams/{team_id}/home-doc",
        json={"url": "https://evil.test/nope"},
        headers=token_leader,
    )
    assert r.status_code == 422, "junk links are rejected"

    r = await client.patch(
        f"/api/teams/{team_id}/home-doc", json={"url": None}, headers=token_leader
    )
    assert r.status_code == 200
    assert r.json()["home_doc_url"] is None


async def test_core_member_may_set_home_doc(client, seeded, token_member):
    """Deliberate, and not to be tightened: ministry leaders here are often
    elderly, so the group that may keep a public page current is widened on
    purpose (api/teams.py:set_home_doc)."""
    from volunteerdb.models import TeamRole
    from volunteerdb.services import memberships

    async with db_session() as session:
        ok(
            await memberships.assign(
                session, None, seeded["volunteer_id"], seeded["team_id"], TeamRole.core
            )
        )
    r = await client.patch(
        f"/api/teams/{seeded['team_id']}/home-doc",
        json={"url": "https://docs.google.com/document/d/core1"},
        headers=token_member,
    )
    assert r.status_code == 200, "core team members may manage the home page"


async def test_page_rows_cascade_with_their_team(database):
    team_id = await _publish("Ephemeral")
    async with db_session() as session:
        session.add(
            TeamPageImage(
                team_id=team_id, seq=1, image=_png(), content_type="image/png"
            )
        )
    async with db_session() as session:
        ok(await teams.delete(session, None, team_id))
    async with db_session() as session:
        assert await session.get(TeamPage, team_id) is None
        assert await session.get(TeamPageImage, (team_id, 1)) is None
