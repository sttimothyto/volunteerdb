"""Public ministry pages: doc-link validation, sanitization, fetch, routes.

The /ministries/ pages are the app's only anonymous surface beyond /login —
they must serve without a session, show only published active teams, and
never serve unsanitized doc HTML.
"""

import httpx
import pytest

from volunteerdb.db import db_session
from volunteerdb.models import TeamPage
from volunteerdb.services import pages, teams

DOC_HTML = (
    "<html><head><meta content='text/html'>"
    "<style type='text/css'>.c1{font-weight:700}</style></head>"
    "<body class='doc'><p class='c1' onclick='alert(1)'>Welcome volunteers</p>"
    "<script>evil()</script><iframe src='https://evil.test'></iframe></body></html>"
)


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
    assert "<style>.c1{font-weight:700}</style>" in clean, "head styles carried over"
    assert 'class="c1"' in clean and "Welcome volunteers" in clean
    assert "script" not in clean and "evil()" not in clean
    assert "iframe" not in clean
    assert "onclick" not in clean


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
        team = await teams.create(session, "Choir")
        await pages.set_home_doc_url(
            session, team.id, "https://docs.google.com/document/d/abc123/edit"
        )
        assert team.home_doc_url == "https://docs.google.com/document/d/abc123/edit"

        with pytest.raises(ValueError):
            await pages.set_home_doc_url(session, team.id, "https://evil.test/doc")

        await pages.set_home_doc_url(session, team.id, None)
        assert team.home_doc_url is None

        with pytest.raises(LookupError):
            await pages.set_home_doc_url(session, 99999, None)


# --- fetch_and_store -------------------------------------------------------


def _doc_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _team_with_doc(name="Choir", doc_id="abc123"):
    async with db_session() as session:
        team = await teams.create(session, name)
        await pages.set_home_doc_url(
            session, team.id, f"https://docs.google.com/document/d/{doc_id}/edit"
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
        assert page.status == "error" and "over 2 MB" in page.error


# --- the public routes -----------------------------------------------------


async def _publish(name: str, html: str = "<p>hello</p>", active=True) -> int:
    async with db_session() as session:
        team = await teams.create(session, name)
        await pages.set_home_doc_url(
            session, team.id, "https://docs.google.com/document/d/x" + str(team.id)
        )
        session.add(TeamPage(team_id=team.id, html=html, status="ok"))
        if not active:
            await teams.update(session, team.id, is_active=False)
        return team.id


async def test_public_index_lists_only_published_active_teams(real_app_client):
    await _publish("Choir")
    await _publish("Closed Ministry", active=False)
    async with db_session() as session:
        await teams.create(session, "No Page Team")

    r = await real_app_client.get("/ministries/", follow_redirects=False)
    assert r.status_code == 200, "anonymous — no login redirect"
    body = r.text
    assert "Choir" in body and "/ministries/choir.html" in body
    assert "Closed Ministry" not in body, "inactive teams are unpublished"
    assert "No Page Team" not in body


async def test_public_team_page_serves_cached_html(real_app_client):
    await _publish("Choir", html="<p class='c1'>Practice is Thursday</p>")

    r = await real_app_client.get("/ministries/choir.html", follow_redirects=False)
    assert r.status_code == 200
    assert "Practice is Thursday" in r.text
    assert "Choir" in r.text
    assert r.headers["cache-control"] == "public, max-age=300"

    r = await real_app_client.get("/ministries/no-such-team.html")
    assert r.status_code == 404

    r = await real_app_client.get("/ministries", follow_redirects=False)
    assert r.status_code in (302, 307) and "/ministries/" in r.headers["location"]


async def test_unpublished_team_page_404s(real_app_client):
    async with db_session() as session:
        await teams.create(session, "Quiet Team")
    r = await real_app_client.get("/ministries/quiet-team.html")
    assert r.status_code == 404


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
    from volunteerdb.models import TeamRole
    from volunteerdb.services import memberships

    async with db_session() as session:
        await memberships.assign(
            session, seeded["volunteer_id"], seeded["team_id"], TeamRole.core
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
        await teams.delete(session, team_id)
    async with db_session() as session:
        assert await session.get(TeamPage, team_id) is None
