"""The nightly page-fetch job: batch isolation and skip rules."""

import httpx

from volunteerdb.db import db_session
from volunteerdb.jobs import fetch_pages
from volunteerdb.models import TeamPage
from volunteerdb.services import pages, teams

from tests.fp_helpers import ok

GOOD = "<html><head></head><body><p>Doc for {doc_id}</p></body></html>"


def _mock_transport(monkeypatch, handler) -> None:
    """Every AsyncClient the job constructs gets the mock transport
    (the test_mail.py pattern)."""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_pages.httpx, "AsyncClient", factory)


async def _team(name: str, doc_id: str | None, active: bool = True) -> int:
    async with db_session() as session:
        team = ok(await teams.create(session, None, name))
        if doc_id is not None:
            ok(
                await pages.set_home_doc_url(
                    session,
                    None,
                    team.id,
                    f"https://docs.google.com/document/d/{doc_id}",
                )
            )
        if not active:
            ok(await teams.update(session, None, team.id, is_active=False))
        return team.id


async def test_one_bad_doc_does_not_poison_the_batch(database, monkeypatch, env):
    good_id = await _team("Choir", "goodoc")
    bad_id = await _team("Ushers", "badoc")

    def handler(request: httpx.Request) -> httpx.Response:
        doc_id = request.url.path.split("/")[3]
        if doc_id == "badoc":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=GOOD.format(doc_id=doc_id))

    _mock_transport(monkeypatch, handler)
    assert await fetch_pages.main(env) == 0, "per-team failures are not job failures"

    async with db_session() as session:
        good = await session.get(TeamPage, good_id)
        assert good.status == "ok" and "Doc for goodoc" in good.html
        bad = await session.get(TeamPage, bad_id)
        assert bad.status == "error" and "500" in bad.error


async def test_second_run_with_unchanged_docs_succeeds_and_keeps_pages(
    database, monkeypatch, env
):
    team_id = await _team("Choir", "gooddoc")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=GOOD.format(doc_id="gooddoc"))

    _mock_transport(monkeypatch, handler)
    assert await fetch_pages.main(env) == 0
    assert await fetch_pages.main(env) == 0, "an all-unchanged night is a success"

    async with db_session() as session:
        page = await session.get(TeamPage, team_id)
        assert page.status == "ok" and "Doc for gooddoc" in page.html


async def test_job_skips_inactive_and_doc_less_teams(database, monkeypatch, env):
    no_doc = await _team("No Doc", None)
    inactive = await _team("Gone", "gonedoc", active=False)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=GOOD.format(doc_id="x"))

    _mock_transport(monkeypatch, handler)
    assert await fetch_pages.main(env) == 0

    assert calls == [], "nothing to fetch"
    async with db_session() as session:
        assert await session.get(TeamPage, no_doc) is None
        assert await session.get(TeamPage, inactive) is None
