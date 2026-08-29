"""services.gcal on the wire: the request shapes Google sees, how its answers
are read, and the sharing check's fix-or-report split. Every AsyncClient is
routed through a MockTransport (the test_gsheets.py idiom)."""

from datetime import UTC, datetime

import httpx
import pytest

from volunteerdb.config import settings
from volunteerdb.services import gcal, google_api

pytestmark = pytest.mark.pure

CID = "abc123@group.calendar.google.com"
OWNER = {
    "id": "user:admin@example.org",
    "role": "owner",
    "scope": {"type": "user", "value": "admin@example.org"},
}
PUBLIC = {"id": "default", "role": "reader", "scope": {"type": "default"}}


def _transport(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(record)
        return real(*args, **kwargs)

    monkeypatch.setattr(gcal.httpx, "AsyncClient", factory)
    return seen


def _no_sleep(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(google_api.asyncio, "sleep", fake_sleep)
    return slept


def _json(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)


# --- the token ------------------------------------------------------------------


def test_enabled_needs_only_the_parish_token(monkeypatch):
    assert not gcal.enabled(), "dev default: unset"
    cfg = settings().model_copy(
        update={
            "sheets_client_id": "cid",
            "sheets_client_secret": "secret",
            "sheets_refresh_token": "refresh",
        }
    )
    monkeypatch.setattr(google_api, "settings", lambda: cfg)
    assert gcal.enabled(), "no folder id, no calendar id: the calendar makes itself"


async def test_mint_token_uses_the_sheets_credentials(monkeypatch):
    cfg = settings().model_copy(
        update={
            "sheets_client_id": "cid",
            "sheets_client_secret": "secret",
            "sheets_refresh_token": "refresh",
        }
    )
    monkeypatch.setattr(google_api, "settings", lambda: cfg)
    seen = _transport(
        monkeypatch, lambda r: httpx.Response(200, json={"access_token": "tok"})
    )
    assert await gcal.mint_token() == "tok"
    body = seen[0].content.decode()
    assert "refresh_token=refresh" in body and "client_id=cid" in body


async def test_a_failed_mint_is_a_gcal_error(monkeypatch):
    _no_sleep(monkeypatch)
    _transport(monkeypatch, lambda r: httpx.Response(401, json={}))
    with pytest.raises(gcal.GcalError, match="token mint failed: HTTP 401"):
        await gcal.mint_token()


# --- the calendar ---------------------------------------------------------------


async def test_create_calendar_names_it_for_the_parish(monkeypatch):
    cfg = settings().model_copy(update={"org_name": "St. Timothy's"})
    monkeypatch.setattr(gcal, "settings", lambda: cfg)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"id": CID}))
    assert await gcal.create_calendar("tok") == CID
    req = seen[0]
    assert req.method == "POST" and str(req.url).endswith("/calendar/v3/calendars")
    assert req.headers["authorization"] == "Bearer tok"
    body = _json(req)
    assert body["summary"] == "St. Timothy's events"
    assert body["timeZone"] == cfg.timezone


async def test_create_calendar_is_not_replayed_after_a_5xx(monkeypatch):
    """A 500 can arrive after Google made the calendar; replaying would make
    a second one nobody remembers."""
    _no_sleep(monkeypatch)
    seen = _transport(monkeypatch, lambda r: httpx.Response(500, json={}))
    with pytest.raises(gcal.GcalError, match="calendar create failed: HTTP 500"):
        await gcal.create_calendar("tok")
    assert len(seen) == 1


async def test_calendar_exists_reads_404_as_gone(monkeypatch):
    answers = iter([404, 200])
    _transport(monkeypatch, lambda r: httpx.Response(next(answers), json={"id": CID}))
    assert await gcal.calendar_exists("tok", CID) is False
    assert await gcal.calendar_exists("tok", CID) is True


async def test_calendar_exists_does_not_mistake_an_outage_for_deletion(monkeypatch):
    """A 503 must not make the sync create a replacement calendar."""
    _no_sleep(monkeypatch)
    _transport(monkeypatch, lambda r: httpx.Response(503, json={}))
    with pytest.raises(gcal.GcalError):
        await gcal.calendar_exists("tok", CID)


# --- sharing ----------------------------------------------------------------------


async def test_a_clean_acl_has_no_problems_and_changes_nothing(monkeypatch):
    seen = _transport(
        monkeypatch, lambda r: httpx.Response(200, json={"items": [OWNER, PUBLIC]})
    )
    assert await gcal.verify_readonly("tok", CID) == []
    assert [r.method for r in seen] == ["GET"]


async def test_a_missing_public_rule_is_added(monkeypatch):
    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, json={"items": [OWNER]})
        return httpx.Response(200, json=PUBLIC)

    seen = _transport(monkeypatch, handler)
    assert await gcal.verify_readonly("tok", CID) == []
    post = [r for r in seen if r.method == "POST"]
    assert len(post) == 1 and str(post[0].url).endswith(
        f"/{CID.replace('@', '%40')}/acl"
    )
    assert _json(post[0]) == {"role": "reader", "scope": {"type": "default"}}


async def test_a_downgraded_public_rule_is_repaired(monkeypatch):
    busy = {**PUBLIC, "role": "freeBusyReader"}

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, json={"items": [OWNER, busy]})
        return httpx.Response(200, json=PUBLIC)

    seen = _transport(monkeypatch, handler)
    assert await gcal.verify_readonly("tok", CID) == []
    patch = [r for r in seen if r.method == "PATCH"]
    assert len(patch) == 1 and str(patch[0].url).endswith("/acl/default")
    assert _json(patch[0]) == {"role": "reader"}


async def test_other_writers_are_reported_not_removed(monkeypatch):
    extra = [
        {"role": "writer", "scope": {"type": "user", "value": "helper@example.org"}},
        {"role": "owner", "scope": {"type": "user", "value": "second@example.org"}},
        {"role": "reader", "scope": {"type": "user", "value": "reader@example.org"}},
        {"role": "writer", "scope": {"type": "domain", "value": "example.org"}},
    ]
    seen = _transport(
        monkeypatch,
        lambda r: httpx.Response(200, json={"items": [OWNER, PUBLIC, *extra]}),
    )
    problems = await gcal.verify_readonly("tok", CID)
    assert problems == [
        "user helper@example.org may write",
        "user second@example.org may own",
        "domain example.org may write",
    ], "a read-only grant to one person is nobody's business but the parish's"
    assert [r.method for r in seen] == ["GET"], "nothing was deleted"


async def test_acl_listing_follows_pages(monkeypatch):
    pages = iter(
        [
            {"items": [OWNER], "nextPageToken": "p2"},
            {"items": [PUBLIC]},
        ]
    )
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json=next(pages)))
    assert await gcal.verify_readonly("tok", CID) == []
    assert seen[1].url.params["pageToken"] == "p2"


# --- events -----------------------------------------------------------------------


async def test_insert_targets_the_given_calendar(monkeypatch):
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"id": "g1"}))
    assert await gcal.insert("tok", CID, {"summary": "Mass"}) == "g1"
    assert str(seen[0].url).endswith(
        "/calendars/abc123%40group.calendar.google.com/events"
    )


async def test_delete_treats_gone_as_done(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(410, json={}))
    await gcal.delete("tok", CID, "g1")  # no raise


async def test_a_throttled_insert_is_retried(monkeypatch):
    slept = _no_sleep(monkeypatch)
    answers = iter([429, 200])

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(next(answers), json={"id": "g1"})

    seen = _transport(monkeypatch, handler)
    assert await gcal.insert("tok", CID, {"summary": "Mass"}) == "g1"
    assert len(seen) == 2 and slept == [google_api.RETRY_BASE_DELAY]


async def test_unmanaged_listing_drops_the_marked_entries(monkeypatch):
    items = [
        {"id": "ours", "extendedProperties": {"private": {"vdb_managed": "1"}}},
        {"id": "byhand", "summary": "Retreat"},
        {"id": "other-app", "extendedProperties": {"private": {"x": "1"}}},
    ]
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"items": items}))
    since = datetime(2026, 1, 1, tzinfo=UTC)
    found = await gcal.list_unmanaged("tok", CID, since)
    assert [i["id"] for i in found] == ["byhand", "other-app"]
    params = seen[0].url.params
    assert "privateExtendedProperty" not in params, "the absence cannot be filtered on"
    assert params["singleEvents"] == "true" and params["timeMin"].startswith(
        "2026-01-01"
    )


async def test_managed_listing_filters_on_the_marker(monkeypatch):
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"items": []}))
    await gcal.list_managed("tok", CID, datetime(2026, 1, 1, tzinfo=UTC))
    assert seen[0].url.params["privateExtendedProperty"] == "vdb_managed=1"
