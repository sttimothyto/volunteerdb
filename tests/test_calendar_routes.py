"""The three feeds and the reset form, through the real app: who may fetch
what, what each carries, the headers a calendar client relies on, and that
the personal token stays out of the request log."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from volunteerdb.db import db_session
from volunteerdb.main import redact_path
from volunteerdb.models import TeamRole
from volunteerdb.services import events as event_service
from volunteerdb.services import memberships, teams, users, volunteers

from tests.fp_helpers import ok

TZ = ZoneInfo("America/Toronto")


def _at(days: int, hour: int) -> datetime:
    return datetime.combine(date.today() + timedelta(days=days), time(hour), TZ)


async def _seed() -> dict:
    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        choir = ok(await teams.create(session, None, "Choir"))
        mia = ok(
            await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
        )
        ok(await memberships.assign(session, None, mia.id, liturgy.id, TeamRole.member))
        mia_u, _ = await users.create(session, "mia@example.org", volunteer_id=mia.id)

        async def event(team_id: int, title: str, day: int) -> int:
            created = await event_service.create_event(
                session,
                None,
                team_id=team_id,
                title=title,
                starts_at=_at(day, 10),
                ends_at=_at(day, 12),
                location="Main church",
                created_by=None,
            )
            return created[0].id

        mass = await event(liturgy.id, "Sunday Mass", 7)
        vespers = await event(liturgy.id, "Vespers", 8)
        practice = await event(choir.id, "Choir practice", 9)
        cancelled = await event(liturgy.id, "Cancelled thing", 10)
        await event_service.cancel_event(session, None, cancelled, cancelled_by=None)
        detail = await event_service.detail(session, None, mass)
        await event_service.sign_up(
            session, None, slot_id=detail.slots[0].slot.id, volunteer_id=mia.id
        )
        token = await users.ensure_calendar_token(session, mia_u.id)
        return {
            "mia_u": mia_u.id,
            "token": token,
            "mass": mass,
            "vespers": vespers,
            "practice": practice,
        }


async def test_the_parish_feed_is_public_and_carries_every_team(real_app_client):
    ids = await _seed()
    r = await real_app_client.get("/calendar/parish.ics", follow_redirects=False)
    assert r.status_code == 200, "anonymous: what the public Google calendar shows"
    assert r.headers["content-type"].startswith("text/calendar")
    assert r.headers["cache-control"] == "public, max-age=900"
    body = r.text
    for title in ("Sunday Mass", "Vespers", "Choir practice"):
        assert f"SUMMARY:{title}" in body
    assert "Cancelled thing" not in body
    assert f"UID:vdb-event-{ids['mass']}@" in body
    assert "URL:http://test/events/" in body or "/events/" in body


async def test_the_personal_feed_is_the_token_holders_duties(real_app_client):
    ids = await _seed()
    r = await real_app_client.get(f"/calendar/mine/{ids['token']}.ics")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, max-age=900"
    assert "SUMMARY:Sunday Mass — Volunteers" in r.text
    assert "Vespers" not in r.text and "Choir practice" not in r.text

    r = await real_app_client.get("/calendar/mine/not-a-token.ics")
    assert r.status_code == 404, "a wrong token is a 404, not a login redirect"


async def test_the_download_needs_the_session_and_names_the_file(real_app_client):
    ids = await _seed()
    r = await real_app_client.get("/calendar/mine.ics", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]

    await real_app_client.get(f"/login-dev/{ids['mia_u']}")
    r = await real_app_client.get("/calendar/mine.ics")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="my-duties.ics"'
    assert r.headers["cache-control"] == "private, no-store"
    assert "SUMMARY:Sunday Mass — Volunteers" in r.text


async def test_resetting_the_address_darkens_the_old_one(real_app_client):
    ids = await _seed()
    r = await real_app_client.post("/calendar/mine/reset", follow_redirects=False)
    assert r.status_code == 401, "no session: nothing to reset"

    await real_app_client.get(f"/login-dev/{ids['mia_u']}")
    r = await real_app_client.post(
        "/calendar/mine/reset",
        headers={"Referer": "http://test/account"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/account"
    old = await real_app_client.get(f"/calendar/mine/{ids['token']}.ics")
    assert old.status_code == 404
    async with db_session() as session:
        new_token = await users.ensure_calendar_token(session, ids["mia_u"])
    assert new_token != ids["token"]
    assert (
        await real_app_client.get(f"/calendar/mine/{new_token}.ics")
    ).status_code == 200

    r = await real_app_client.post(
        "/calendar/mine/reset",
        headers={"Referer": "https://elsewhere.example/steal"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/events", (
        "a foreign referer is not a redirect target"
    )


def test_the_token_never_reaches_the_request_log():
    assert redact_path("/calendar/mine/abc123.ics") == "/calendar/mine/<redacted>"
    assert redact_path("/calendar/parish.ics") == "/calendar/parish.ics"
