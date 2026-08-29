"""The events API: creation with copy-forward, actor scoping, RSVPs and
sign-ups, the manager-only mutations, substitution claims, attendance
exceptions and the derived hours endpoint.

Unlike the GUI these routes send no mail — asserted nowhere here because
nothing patches mail: a send attempt in dev mode would only print.
"""

from datetime import UTC, date, datetime, time, timedelta

from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

from .conftest import _token
from tests import mint
from tests.conftest import db_session
from tests.fp_helpers import ok


def _iso(day_offset: int, hour: int) -> str:
    day = date.today() + timedelta(days=day_offset)
    return datetime.combine(day, time(hour), UTC).isoformat()


def _payload(team_id: int, **overrides) -> dict:
    body = {
        "team_id": team_id,
        "title": "Sunday Mass",
        "starts_at": _iso(7, 14),
        "ends_at": _iso(7, 16),
        "slots": [{"name": "Lector", "capacity": 1}, {"name": "Usher"}],
    }
    body.update(overrides)
    return body


async def _second_member(client, seeded) -> tuple[int, dict]:
    """Another Liturgy member with an account; returns (volunteer_id, header)."""
    async with db_session() as session:
        v = ok(
            await volunteers.create(session, None, "Noor", "Reader", "noor@example.org")
        )
        ok(
            await memberships.assign(
                session, None, v.id, seeded["team_id"], TeamRole.member
            )
        )
        ok(
            await users.create(
                session,
                "noor@example.org",
                volunteer_id=v.id,
                password="noor-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
    return v.id, await _token(client, "noor@example.org", "noor-pass-phrase")


async def test_leader_creates_repeating_event(client, seeded, token_leader):
    body = _payload(seeded["team_id"])
    body["repeat_weekly_until"] = (date.today() + timedelta(days=21)).isoformat()
    r = await client.post("/api/events", json=body, headers=token_leader)
    assert r.status_code == 201, r.text
    assert len(r.json()) == 3
    assert all(e["status"] == "scheduled" for e in r.json())


async def test_member_cannot_create_or_assign(client, seeded, token_member):
    r = await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_member
    )
    assert r.status_code == 403


async def test_listing_is_scoped_and_detail_is_gated(
    client, seeded, token_leader, token_member, token_admin
):
    await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_leader
    )
    async with db_session() as session:
        other = ok(await teams.create(session, None, "Garden Guild"))
    r = await client.post(
        "/api/events",
        json=_payload(other.id, title="Weeding bee"),
        headers=token_admin,
    )
    other_event = r.json()[0]["id"]

    r = await client.get("/api/events", headers=token_member)
    assert [e["event"]["title"] for e in r.json()] == ["Sunday Mass"]
    assert r.json()[0]["path"] == "Liturgy"
    r = await client.get("/api/events", headers=token_admin)
    assert {e["event"]["title"] for e in r.json()} == {"Sunday Mass", "Weeding bee"}

    r = await client.get(f"/api/events/{other_event}", headers=token_member)
    assert r.status_code == 403, "no roster-name rights on that team"


async def test_rsvp_signup_capacity_and_withdraw(
    client, seeded, token_leader, token_member
):
    r = await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_leader
    )
    event_id = r.json()[0]["id"]

    r = await client.put(
        f"/api/events/{event_id}/rsvp",
        json={"available": True, "note": "after 9 works"},
        headers=token_member,
    )
    assert r.status_code == 204

    detail = (await client.get(f"/api/events/{event_id}", headers=token_member)).json()
    assert [rv["note"] for rv in detail["rsvps"]] == ["after 9 works"]
    lector = next(s for s in detail["slots"] if s["slot"]["name"] == "Lector")

    r = await client.post(
        f"/api/events/{event_id}/slots/{lector['slot']['id']}/assignments",
        json={},
        headers=token_member,
    )
    assert r.status_code == 201
    assignment = r.json()
    assert assignment["kind"] == "signup"

    # capacity 1: the leader cannot add a second lector
    async with db_session() as session:
        extra = ok(
            await volunteers.create(session, None, "Iris", "Extra", "iris@example.org")
        )
        ok(
            await memberships.assign(
                session, None, extra.id, seeded["team_id"], TeamRole.member
            )
        )
    r = await client.post(
        f"/api/events/{event_id}/slots/{lector['slot']['id']}/assignments",
        json={"volunteer_id": extra.id},
        headers=token_leader,
    )
    assert r.status_code == 422 and "full" in r.text

    # a member cannot schedule someone else
    r = await client.post(
        f"/api/events/{event_id}/slots/{lector['slot']['id']}/assignments",
        json={"volunteer_id": extra.id},
        headers=token_member,
    )
    assert r.status_code == 403

    r = await client.delete(
        f"/api/events/assignments/{assignment['id']}", headers=token_member
    )
    assert r.status_code == 204


async def test_sub_request_and_claim_move_the_assignment(
    client, seeded, token_leader, token_member
):
    noor_id, token_noor = await _second_member(client, seeded)
    r = await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_leader
    )
    event_id = r.json()[0]["id"]
    detail = (await client.get(f"/api/events/{event_id}", headers=token_member)).json()
    usher = next(s for s in detail["slots"] if s["slot"]["name"] == "Usher")
    a = (
        await client.post(
            f"/api/events/{event_id}/slots/{usher['slot']['id']}/assignments",
            json={},
            headers=token_member,
        )
    ).json()

    r = await client.post(
        f"/api/events/assignments/{a['id']}/sub-request",
        json={"note": "away that Sunday"},
        headers=token_member,
    )
    assert r.status_code == 201
    sub_id = r.json()["id"]

    # noor may not withdraw someone else's request…
    r = await client.post(
        f"/api/events/sub-requests/{sub_id}/cancel", headers=token_noor
    )
    assert r.status_code == 403
    # …but may claim it
    r = await client.post(
        f"/api/events/sub-requests/{sub_id}/claim", headers=token_noor
    )
    assert r.status_code == 200
    assert r.json()["status"] == "claimed"
    assert r.json()["claimed_by_volunteer_id"] == noor_id

    detail = (await client.get(f"/api/events/{event_id}", headers=token_member)).json()
    usher = next(s for s in detail["slots"] if s["slot"]["name"] == "Usher")
    assert [e["volunteer_id"] for e in usher["entries"]] == [noor_id]
    assert usher["entries"][0]["kind"] == "sub"


async def test_attendance_flow_and_hours(client, seeded, token_leader, token_member):
    r = await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_leader
    )
    event_id = r.json()[0]["id"]
    detail = (await client.get(f"/api/events/{event_id}", headers=token_member)).json()
    usher = next(s for s in detail["slots"] if s["slot"]["name"] == "Usher")
    a = (
        await client.post(
            f"/api/events/{event_id}/slots/{usher['slot']['id']}/assignments",
            json={},
            headers=token_member,
        )
    ).json()

    # attendance before the event ends: 422
    r = await client.patch(
        f"/api/events/assignments/{a['id']}/attendance",
        json={"attended": False, "hours": None},
        headers=token_leader,
    )
    assert r.status_code == 422

    # the leader backdates the event (times may be corrected on past events)
    r = await client.patch(
        f"/api/events/{event_id}",
        json={"starts_at": _iso(-7, 14), "ends_at": _iso(-7, 16)},
        headers=token_leader,
    )
    assert r.status_code == 200

    detail = (await client.get(f"/api/events/{event_id}", headers=token_leader)).json()
    assert detail["attendance"] is not None, "past + manager: attendance appears"
    row = detail["attendance"][0]
    assert (row["attended"], row["hours"], row["overridden"]) == (True, 2.0, False)

    r = await client.patch(
        f"/api/events/assignments/{a['id']}/attendance",
        json={"attended": True, "hours": 3.5},
        headers=token_member,
    )
    assert r.status_code == 403, "members do not record attendance"
    r = await client.patch(
        f"/api/events/assignments/{a['id']}/attendance",
        json={"attended": True, "hours": 3.5},
        headers=token_leader,
    )
    assert r.status_code == 200
    assert (r.json()["hours"], r.json()["overridden"]) == (3.5, True)

    r = await client.get(
        f"/api/volunteers/{seeded['volunteer_id']}/hours", headers=token_member
    )
    assert r.status_code == 200, "self may read their own record"
    assert r.json() == {
        "volunteer_id": seeded["volunteer_id"],
        "total_hours": 3.5,
        "events_attended": 1,
    }

    # a stranger with no shared team may not
    async with db_session() as session:
        ok(
            await users.create(
                session,
                "stranger@example.org",
                password="stranger-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
    token_stranger = await _token(
        client, "stranger@example.org", "stranger-pass-phrase"
    )
    r = await client.get(
        f"/api/volunteers/{seeded['volunteer_id']}/hours", headers=token_stranger
    )
    assert r.status_code == 403


async def test_cancel_blocks_further_changes(client, seeded, token_leader):
    r = await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_leader
    )
    event_id = r.json()[0]["id"]
    r = await client.post(f"/api/events/{event_id}/cancel", headers=token_leader)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.patch(
        f"/api/events/{event_id}", json={"title": "New name"}, headers=token_leader
    )
    assert r.status_code == 422
    r = await client.post(
        f"/api/events/{event_id}/slots",
        json={"name": "Greeter"},
        headers=token_leader,
    )
    assert r.status_code == 422
    r = await client.get("/api/events", headers=token_leader)
    assert r.json() == [], "cancelled events hide unless include_cancelled"
    r = await client.get("/api/events?include_cancelled=true", headers=token_leader)
    assert len(r.json()) == 1


async def test_slot_description_round_trips_and_clears(client, seeded, token_leader):
    """POST accepts it, GET returns it, PATCH rewords it, explicit null clears
    it — and over the cap is a 422 rather than a 500 out of the driver."""
    r = await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_leader
    )
    event_id = r.json()[0]["id"]

    r = await client.post(
        f"/api/events/{event_id}/slots",
        json={"name": "Greeter", "description": "Main door, from 10:00"},
        headers=token_leader,
    )
    assert r.status_code == 201 and r.json()["description"] == "Main door, from 10:00"
    slot_id = r.json()["id"]

    detail = (await client.get(f"/api/events/{event_id}", headers=token_leader)).json()
    greeter = next(s for s in detail["slots"] if s["slot"]["name"] == "Greeter")
    assert greeter["slot"]["description"] == "Main door, from 10:00"

    r = await client.patch(
        f"/api/events/{event_id}/slots/{slot_id}",
        json={"description": "Side door instead"},
        headers=token_leader,
    )
    assert r.status_code == 200 and r.json()["description"] == "Side door instead"
    assert r.json()["name"] == "Greeter", "a description-only patch leaves the name"

    r = await client.patch(
        f"/api/events/{event_id}/slots/{slot_id}",
        json={"description": None},
        headers=token_leader,
    )
    assert r.status_code == 200 and r.json()["description"] is None

    r = await client.post(
        f"/api/events/{event_id}/slots",
        json={"name": "Cantor", "description": "x" * 301},
        headers=token_leader,
    )
    assert r.status_code == 422


async def test_slot_crud_guards(client, seeded, token_leader, token_member):
    r = await client.post(
        "/api/events", json=_payload(seeded["team_id"]), headers=token_leader
    )
    event_id = r.json()[0]["id"]
    detail = (await client.get(f"/api/events/{event_id}", headers=token_leader)).json()
    lector = next(s for s in detail["slots"] if s["slot"]["name"] == "Lector")
    usher = next(s for s in detail["slots"] if s["slot"]["name"] == "Usher")

    await client.post(
        f"/api/events/{event_id}/slots/{lector['slot']['id']}/assignments",
        json={},
        headers=token_member,
    )
    r = await client.delete(
        f"/api/events/{event_id}/slots/{lector['slot']['id']}", headers=token_leader
    )
    assert r.status_code == 422, "occupied slots cannot be removed"
    r = await client.patch(
        f"/api/events/{event_id}/slots/{usher['slot']['id']}",
        json={"name": "Greeter", "capacity": 4},
        headers=token_leader,
    )
    assert r.status_code == 200 and r.json()["capacity"] == 4
    r = await client.delete(
        f"/api/events/{event_id}/slots/{usher['slot']['id']}", headers=token_leader
    )
    assert r.status_code == 204
    r = await client.delete(
        f"/api/events/{event_id}/slots/{lector['slot']['id']}", headers=token_member
    )
    assert r.status_code == 403


async def test_repeat_series_signup_via_api(client, seeded, token_leader, token_member):
    body = _payload(seeded["team_id"])
    body["repeat_weekly_until"] = (date.today() + timedelta(days=21)).isoformat()
    r = await client.post("/api/events", json=body, headers=token_leader)
    assert r.status_code == 201
    weeks = r.json()
    assert weeks[0]["series_id"] is not None
    assert all(e["series_id"] == weeks[0]["series_id"] for e in weeks)

    async def usher_slot(event_id: int) -> dict:
        detail = (
            await client.get(f"/api/events/{event_id}", headers=token_member)
        ).json()
        return next(s for s in detail["slots"] if s["slot"]["name"] == "Usher")

    first = weeks[0]["id"]
    slot_id = (await usher_slot(first))["slot"]["id"]
    r = await client.post(
        f"/api/events/{first}/slots/{slot_id}/assignments",
        json={"repeat_series": True},
        headers=token_member,
    )
    assert r.status_code == 201, r.text
    for week in weeks[1:]:
        assert len((await usher_slot(week["id"]))["entries"]) == 1, (
            "the sign-up copied itself onto the later weeks"
        )

    # a manager scheduling somebody cannot ask for the copy-forward
    r = await client.post(
        f"/api/events/{first}/slots/{slot_id}/assignments",
        json={"volunteer_id": seeded["volunteer_id"], "repeat_series": True},
        headers=token_leader,
    )
    assert r.status_code == 422
