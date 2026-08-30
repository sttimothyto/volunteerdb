"""The manual's search endpoint through the real app: who may call it, what
it answers, and that the audience gate holds. The manual is two pages in a
temporary directory and the model is switched off, so nothing here reads
docs/_build or fetches anything."""

import pytest
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.config import settings
from volunteerdb.services import users

from tests import mint
from tests.conftest import SIM_MAIN, db_session
from tests.fp_helpers import ok


def _page(slug: str, title: str, body: str) -> str:
    return (
        f"<html><head><title>{title} - VolunteerDB documentation</title></head>"
        f'<body><article role="main"><section id="{slug}">'
        f'<h1>{title}<a class="headerlink" href="#{slug}">¶</a></h1>'
        f"<p>{body}</p></section></article></body></html>"
    )


@pytest.fixture
async def manual_client(database, tmp_path, monkeypatch):
    docs = tmp_path / "html"
    (docs / "guide" / "how-to").mkdir(parents=True)
    (docs / "how-to").mkdir()
    (docs / "guide" / "how-to" / "rsvp.html").write_text(
        _page("rsvp", "RSVP to a shift", "Click Yes to RSVP to a shift you are on.")
    )
    (docs / "how-to" / "deploy.html").write_text(
        _page("deploy", "Deploy", "Deploy the image to the host with pyinfra.")
    )
    monkeypatch.setenv("VDB_DOCS_DIR", str(docs))
    # keyword-only: the tests never fetch the model, and must not depend on
    # whether this checkout ran `make model`
    monkeypatch.setenv("VDB_MANUAL_MODEL_DIR", "")
    # settings() is lru_cached and env.build() reads it; the simulated app
    # must see these two, and the next test must not
    settings.cache_clear()
    try:
        async with user_simulation(main_file=SIM_MAIN) as user:
            yield user.http_client
    finally:
        settings.cache_clear()


async def _signed_in(client) -> None:
    async with db_session() as session:
        user, _ = ok(
            await users.create(session, "mia@example.org", invite=mint.fresh_invite())
        )
    await client.get(f"/login-dev/{user.id}")


async def test_anonymous_callers_are_sent_to_login(manual_client):
    r = await manual_client.get("/manual/_search?q=rsvp", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers["location"]


async def test_a_signed_in_reader_gets_the_guide_and_not_the_technical_manual(
    manual_client,
):
    await _signed_in(manual_client)
    r = await manual_client.get("/manual/_search?q=rsvp")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["query"] == "rsvp"
    assert body["results"][0]["url"] == "/manual/guide/how-to/rsvp.html"
    assert body["results"][0]["title"] == "RSVP to a shift"
    assert body["results"][0]["audience"] == "user"
    assert "RSVP" in body["results"][0]["snippet"]
    assert set(body["results"][0]) == {"title", "section", "url", "snippet", "audience"}

    r = await manual_client.get("/manual/_search?q=deploy")
    assert r.json()["results"] == [], "the technical manual is hidden by default"
    r = await manual_client.get("/manual/_search?q=deploy&audience=all")
    assert [hit["url"] for hit in r.json()["results"]] == ["/manual/how-to/deploy.html"]
    assert r.json()["results"][0]["audience"] == "dev"


async def test_the_query_is_normalised_and_bounded(manual_client):
    await _signed_in(manual_client)
    r = await manual_client.get("/manual/_search?q=%20%20rsvp%20%20%20shift%20")
    assert r.status_code == 200 and r.json()["query"] == "rsvp shift"

    for bad in ("", "%20%20", "x" * 201, "x&audience=staff", "x&audience="):
        r = await manual_client.get(f"/manual/_search?q={bad}")
        assert r.status_code == 400, bad
        assert r.headers["cache-control"] == "no-store"
        assert "detail" in r.json()
    r = await manual_client.get("/manual/_search")
    assert r.status_code == 400, "a missing q is our 400, not FastAPI's 422"
