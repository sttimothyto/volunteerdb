"""services.gcal on the wire: the request shapes Google sees, how its answers
are read, and the sharing check's fix-or-report split. Every call gets a
client routed through a MockTransport (the test_gsheets.py idiom) — what
env.HttpClients hands the job — so nothing is patched but the backoff."""

from datetime import UTC, datetime

import httpx
import pytest

from volunteerdb.errors import External
from volunteerdb.services import gcal, google_api
from volunteerdb.services.google_api import GoogleConfig

from tests.fp_helpers import ok, refused

pytestmark = pytest.mark.pure

CID = "abc123@group.calendar.google.com"
TZ = "America/Toronto"
OWNER = {
    "id": "user:admin@example.org",
    "role": "owner",
    "scope": {"type": "user", "value": "admin@example.org"},
}
PUBLIC = {"id": "default", "role": "reader", "scope": {"type": "default"}}
CFG = GoogleConfig(client_id="cid", client_secret="secret", refresh_token="refresh")


def _client(handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(record)), seen


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


def test_enabled_needs_only_the_parish_token():
    assert not gcal.enabled(GoogleConfig()), "dev default: unset"
    assert gcal.enabled(CFG), "no folder id, no calendar id: the calendar makes itself"
    assert not gcal.enabled(GoogleConfig(client_id="cid", client_secret="secret")), (
        "all three parts, or nothing"
    )


async def test_mint_token_uses_the_sheets_credentials():
    client, seen = _client(lambda r: httpx.Response(200, json={"access_token": "tok"}))
    assert ok(await gcal.mint_token(client, CFG)) == "tok"
    body = seen[0].content.decode()
    assert "refresh_token=refresh" in body and "client_id=cid" in body


async def test_a_failed_mint_is_a_calendar_error(monkeypatch):
    _no_sleep(monkeypatch)
    client, _ = _client(lambda r: httpx.Response(401, json={}))
    err = refused(
        await gcal.mint_token(client, CFG),
        External,
        match="token mint failed: HTTP 401",
    )
    assert err.service == "google calendar", "named for the log"


# --- the calendar ---------------------------------------------------------------


async def test_create_calendar_names_it_for_the_parish():
    client, seen = _client(lambda r: httpx.Response(200, json={"id": CID}))
    name = gcal.calendar_name("St. Timothy's")
    assert name == "St. Timothy's events"
    assert gcal.calendar_name("  ") == "Parish events"
    assert ok(await gcal.create_calendar(client, "tok", name=name, tz=TZ)) == CID
    req = seen[0]
    assert req.method == "POST" and str(req.url).endswith("/calendar/v3/calendars")
    assert req.headers["authorization"] == "Bearer tok"
    body = _json(req)
    assert body["summary"] == "St. Timothy's events"
    assert body["timeZone"] == TZ


async def test_create_calendar_is_not_replayed_after_a_5xx(monkeypatch):
    """A 500 can arrive after Google made the calendar; replaying would make
    a second one nobody remembers."""
    _no_sleep(monkeypatch)
    client, seen = _client(lambda r: httpx.Response(500, json={}))
    refused(
        await gcal.create_calendar(client, "tok", name="Parish events", tz=TZ),
        External,
        match="calendar create failed: HTTP 500",
    )
    assert len(seen) == 1


async def test_calendar_exists_reads_404_as_gone():
    answers = iter([404, 200])
    client, _ = _client(lambda r: httpx.Response(next(answers), json={"id": CID}))
    assert ok(await gcal.calendar_exists(client, "tok", CID)) is False
    assert ok(await gcal.calendar_exists(client, "tok", CID)) is True


async def test_calendar_exists_does_not_mistake_an_outage_for_deletion(monkeypatch):
    """A 503 must not make the sync create a replacement calendar."""
    _no_sleep(monkeypatch)
    client, _ = _client(lambda r: httpx.Response(503, json={}))
    refused(await gcal.calendar_exists(client, "tok", CID), External)


# --- sharing ----------------------------------------------------------------------


async def test_a_clean_acl_has_no_problems_and_changes_nothing():
    client, seen = _client(
        lambda r: httpx.Response(200, json={"items": [OWNER, PUBLIC]})
    )
    assert ok(await gcal.verify_readonly(client, "tok", CID)) == []
    assert [r.method for r in seen] == ["GET"]


async def test_a_missing_public_rule_is_added():
    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, json={"items": [OWNER]})
        return httpx.Response(200, json=PUBLIC)

    client, seen = _client(handler)
    assert ok(await gcal.verify_readonly(client, "tok", CID)) == []
    post = [r for r in seen if r.method == "POST"]
    assert len(post) == 1 and str(post[0].url).endswith(
        f"/{CID.replace('@', '%40')}/acl"
    )
    assert _json(post[0]) == {"role": "reader", "scope": {"type": "default"}}


async def test_a_downgraded_public_rule_is_repaired():
    busy = {**PUBLIC, "role": "freeBusyReader"}

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, json={"items": [OWNER, busy]})
        return httpx.Response(200, json=PUBLIC)

    client, seen = _client(handler)
    assert ok(await gcal.verify_readonly(client, "tok", CID)) == []
    patch = [r for r in seen if r.method == "PATCH"]
    assert len(patch) == 1 and str(patch[0].url).endswith("/acl/default")
    assert _json(patch[0]) == {"role": "reader"}


async def test_a_failed_publish_is_the_checks_failure(monkeypatch):
    """The public rule is what makes the calendar public; not managing to set
    it is the run's failure, not a problem list."""
    _no_sleep(monkeypatch)

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, json={"items": [OWNER]})
        return httpx.Response(403, json={})

    client, _ = _client(handler)
    refused(
        await gcal.verify_readonly(client, "tok", CID),
        External,
        match="publish failed: HTTP 403",
    )


async def test_other_writers_are_reported_not_removed():
    extra = [
        {"role": "writer", "scope": {"type": "user", "value": "helper@example.org"}},
        {"role": "owner", "scope": {"type": "user", "value": "second@example.org"}},
        {"role": "reader", "scope": {"type": "user", "value": "reader@example.org"}},
        {"role": "writer", "scope": {"type": "domain", "value": "example.org"}},
    ]
    client, seen = _client(
        lambda r: httpx.Response(200, json={"items": [OWNER, PUBLIC, *extra]})
    )
    problems = ok(await gcal.verify_readonly(client, "tok", CID))
    assert problems == [
        "user helper@example.org may write",
        "user second@example.org may own",
        "domain example.org may write",
    ], "a read-only grant to one person is nobody's business but the parish's"
    assert [r.method for r in seen] == ["GET"], "nothing was deleted"


async def test_the_calendars_own_identity_is_not_a_foreign_owner():
    """A Workspace account's calendar lists the calendar's own id as a
    user-owner ahead of the parish account; neither is anybody else."""
    self_rule = {
        "id": f"user:{CID}",
        "role": "owner",
        "scope": {"type": "user", "value": CID},
    }
    client, seen = _client(
        lambda r: httpx.Response(200, json={"items": [self_rule, OWNER, PUBLIC]})
    )
    assert ok(await gcal.verify_readonly(client, "tok", CID)) == []
    assert [r.method for r in seen] == ["GET"]


async def test_acl_listing_follows_pages():
    pages = iter(
        [
            {"items": [OWNER], "nextPageToken": "p2"},
            {"items": [PUBLIC]},
        ]
    )
    client, seen = _client(lambda r: httpx.Response(200, json=next(pages)))
    assert ok(await gcal.verify_readonly(client, "tok", CID)) == []
    assert seen[1].url.params["pageToken"] == "p2"


# --- events -----------------------------------------------------------------------


async def test_insert_targets_the_given_calendar():
    client, seen = _client(lambda r: httpx.Response(200, json={"id": "g1"}))
    assert ok(await gcal.insert(client, "tok", CID, {"summary": "Mass"})) == "g1"
    assert str(seen[0].url).endswith(
        "/calendars/abc123%40group.calendar.google.com/events"
    )


async def test_delete_treats_gone_as_done():
    client, _ = _client(lambda r: httpx.Response(410, json={}))
    ok(await gcal.delete(client, "tok", CID, "g1"))


async def test_a_throttled_insert_is_retried(monkeypatch):
    slept = _no_sleep(monkeypatch)
    answers = iter([429, 200])

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(next(answers), json={"id": "g1"})

    client, seen = _client(handler)
    assert ok(await gcal.insert(client, "tok", CID, {"summary": "Mass"})) == "g1"
    assert len(seen) == 2 and slept == [google_api.RETRY_BASE_DELAY]


async def test_a_reset_on_a_write_that_must_not_replay_is_an_error(monkeypatch):
    """A 500 may have created the entry; a reset likewise. Neither is replayed
    (a second entry nobody remembers), and both come back as a value."""
    _no_sleep(monkeypatch)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset")

    client, seen = _client(boom)
    refused(
        await gcal.insert(client, "tok", CID, {"summary": "Mass"}),
        External,
        match="insert failed: ConnectError",
    )
    assert len(seen) == 1


async def test_unmanaged_listing_drops_the_marked_entries():
    items = [
        {"id": "ours", "extendedProperties": {"private": {"vdb_managed": "1"}}},
        {"id": "byhand", "summary": "Retreat"},
        {"id": "other-app", "extendedProperties": {"private": {"x": "1"}}},
    ]
    client, seen = _client(lambda r: httpx.Response(200, json={"items": items}))
    since = datetime(2026, 1, 1, tzinfo=UTC)
    found = ok(await gcal.list_unmanaged(client, "tok", CID, since))
    assert [i["id"] for i in found] == ["byhand", "other-app"]
    params = seen[0].url.params
    assert "privateExtendedProperty" not in params, "the absence cannot be filtered on"
    assert params["singleEvents"] == "true" and params["timeMin"].startswith(
        "2026-01-01"
    )


async def test_managed_listing_filters_on_the_marker():
    client, seen = _client(lambda r: httpx.Response(200, json={"items": []}))
    ok(await gcal.list_managed(client, "tok", CID, datetime(2026, 1, 1, tzinfo=UTC)))
    assert seen[0].url.params["privateExtendedProperty"] == "vdb_managed=1"


def test_event_payload_and_the_embed_view_carry_the_parish_zone():
    class Stub:
        id = 7
        title = "Mass"
        starts_at = datetime(2026, 3, 1, 15, tzinfo=UTC)
        ends_at = datetime(2026, 3, 1, 16, tzinfo=UTC)
        location = ""
        description = ""

    payload = gcal.event_payload(Stub(), TZ)  # type: ignore[arg-type]
    assert payload["start"]["timeZone"] == TZ and "location" not in payload
    assert payload["extendedProperties"]["private"] == {
        "vdb_id": "7",
        "vdb_managed": "1",
    }
    assert gcal.embed_url(CID, TZ).endswith("&ctz=America/Toronto")
