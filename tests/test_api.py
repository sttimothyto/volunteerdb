"""JSON API smoke tests over ASGI: auth, CRUD, permissions, as-of."""

import asyncio
from datetime import UTC, date, datetime

from volunteerdb.api.deps import as_of_param
from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import custom_fields as custom_fields_service
from volunteerdb.services import memberships, users, volunteers
from volunteerdb.ui.context import parse_as_of

from tests.conftest import _token


async def test_login_and_me(client, seeded):
    headers = await _token(client, "admin@example.org", "secret-pass-phrase")
    r = await client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["is_admin"] is True

    r = await client.post(
        "/api/auth/login", json={"email": "admin@example.org", "password": "nope"}
    )
    assert r.status_code == 401
    r = await client.get("/api/teams")
    assert r.status_code == 401


async def test_admin_crud_flow(client, seeded):
    headers = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post(
        "/api/volunteers",
        json={"first_name": "New", "last_name": "Person", "email": "new@example.org"},
        headers=headers,
    )
    assert r.status_code == 201
    vid = r.json()["id"]

    r = await client.post(
        "/api/memberships",
        json={"volunteer_id": vid, "team_id": seeded["team_id"], "role": "leader"},
        headers=headers,
    )
    assert r.status_code == 201

    r = await client.get(f"/api/teams/{seeded['team_id']}/roster", headers=headers)
    assert r.status_code == 200
    roster = r.json()
    assert roster[0]["role"] == "leader", "leaders sort first"
    assert {e["volunteer"]["id"] for e in roster} == {vid, seeded["volunteer_id"]}

    r = await client.get(f"/api/volunteers/{vid}/impact", headers=headers)
    assert r.status_code == 200
    assert r.json()[0]["leaders_left"] == 0

    r = await client.get("/api/reports/coverage", headers=headers)
    assert r.status_code == 200
    liturgy = [row for row in r.json() if row["team_id"] == seeded["team_id"]][0]
    assert liturgy["leader"] == 1 and liturgy["missing_second"] is True

    r = await client.get("/api/graph", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert any(n["data"]["type"] == "team" for n in data["nodes"])
    assert any(e["data"].get("leadership") for e in data["edges"])


async def test_member_permissions_enforced(client, seeded):
    headers = await _token(client, "member@example.org", "member-pass-phrase")

    r = await client.get("/api/teams", headers=headers)
    assert r.status_code == 200, "directory is browsable"

    r = await client.post(
        "/api/volunteers", json={"first_name": "X", "last_name": "Y"}, headers=headers
    )
    assert r.status_code == 403

    r = await client.post(
        "/api/memberships",
        json={
            "volunteer_id": seeded["volunteer_id"],
            "team_id": seeded["team_id"],
            "role": "leader",
        },
        headers=headers,
    )
    assert r.status_code == 403

    r = await client.get("/api/reports/coverage", headers=headers)
    assert r.status_code == 403

    # names visible, contacts redacted for a fellow member they can't view fully
    r = await client.get(f"/api/teams/{seeded['team_id']}/roster", headers=headers)
    assert r.status_code == 200


async def test_as_of_time_travel(client, seeded):
    headers = await _token(client, "admin@example.org", "secret-pass-phrase")

    await asyncio.sleep(0.02)
    before = datetime.now(UTC).isoformat()
    await asyncio.sleep(0.02)

    r = await client.patch(
        f"/api/volunteers/{seeded['volunteer_id']}",
        json={"first_name": "Renamed"},
        headers=headers,
    )
    assert r.status_code == 200

    r = await client.get(f"/api/volunteers/{seeded['volunteer_id']}", headers=headers)
    assert r.json()["first_name"] == "Renamed"

    r = await client.get(
        f"/api/volunteers/{seeded['volunteer_id']}",
        params={"as_of": before},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["first_name"] == "Maria", "as-of sees the pre-rename state"


async def test_include_inactive_requires_admin(client, seeded):
    """The GUI restricts the archived-volunteers toggle to admins; the API let
    any signed-in caller enumerate them. Same rule on both surfaces."""
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")
    member = await _token(client, "member@example.org", "member-pass-phrase")

    r = await client.post(
        "/api/volunteers",
        json={"first_name": "Archie", "last_name": "Archived"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    r = await client.patch(
        f"/api/volunteers/{r.json()['id']}", json={"is_active": False}, headers=admin
    )
    assert r.status_code == 200, r.text

    r = await client.get(
        "/api/volunteers", params={"include_inactive": "true"}, headers=admin
    )
    assert r.status_code == 200
    assert "Archived" in [v["last_name"] for v in r.json()]

    r = await client.get(
        "/api/volunteers", params={"include_inactive": "true"}, headers=member
    )
    assert r.status_code == 403, "archived volunteers are admin-only"

    r = await client.get("/api/volunteers", headers=member)
    assert r.status_code == 200
    assert "Archived" not in [v["last_name"] for v in r.json()], (
        "the default listing is unchanged for everyone else"
    )


async def test_a_bare_as_of_date_covers_that_whole_day(client, seeded):
    """A bare date means the END of that day, on both surfaces. The API used to
    annotate as_of as a datetime, so FastAPI parsed '2026-07-30' to midnight
    while the GUI bumped it to 23:59:59 — the same query string returning
    snapshots a day apart."""
    headers = await _token(client, "admin@example.org", "secret-pass-phrase")
    today = date.today().isoformat()

    r = await client.get("/api/volunteers", params={"as_of": today}, headers=headers)
    assert r.status_code == 200, r.text
    assert [v["last_name"] for v in r.json()] == ["Alvarez"], (
        "the seeded volunteer was created today; resolving a bare as_of date to "
        "midnight would hide the whole day's history"
    )

    assert as_of_param(today) == parse_as_of(today), (
        "the GUI and the API must resolve the same query string to the same instant"
    )

    r = await client.get(
        "/api/volunteers", params={"as_of": "not-a-date"}, headers=headers
    )
    assert r.status_code == 422, "garbage is still rejected rather than ignored"


async def test_volunteer_timeline(client, seeded):
    headers = await _token(client, "member@example.org", "member-pass-phrase")
    vid = seeded["volunteer_id"]

    # leave and rejoin so the timeline has one closed and one open spell
    async with db_session() as session:
        m = await memberships.find(session, vid, seeded["team_id"])
        await memberships.remove(session, None, m.id)
    async with db_session() as session:
        await memberships.assign(session, None, vid, seeded["team_id"], TeamRole.core)

    r = await client.get(f"/api/volunteers/{vid}/timeline", headers=headers)
    assert r.status_code == 200
    first, second = r.json()
    assert first["team_name"] == "Liturgy" and first["team_deleted"] is False
    assert first["end"] is not None and first["role"] == "member"
    assert second["end"] is None and second["role"] == "core"
    assert second["role_label"] == "Core team member"
    assert first["segments"] and first["segments"][0]["start"]

    r = await client.get("/api/volunteers/999999/timeline", headers=headers)
    assert r.status_code == 200 and r.json() == []


async def test_custom_fields_flow(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")
    member = await _token(client, "member@example.org", "member-pass-phrase")

    # only admins define fields
    body = {
        "label": "Preferred contact",
        "field_type": "select",
        "options": ["Email", "Phone"],
    }
    r = await client.post("/api/custom-fields", json=body, headers=member)
    assert r.status_code == 403
    r = await client.post("/api/custom-fields", json=body, headers=admin)
    assert r.status_code == 201
    field = r.json()
    assert field["key"] == "preferred_contact"

    # anyone signed in can read the definitions
    r = await client.get("/api/custom-fields", headers=member)
    assert r.status_code == 200 and r.json()[0]["key"] == "preferred_contact"

    # invalid value -> 422; valid -> stored
    vid = seeded["volunteer_id"]
    r = await client.patch(
        f"/api/volunteers/{vid}",
        json={"custom": {"preferred_contact": "Fax"}},
        headers=admin,
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/api/volunteers/{vid}",
        json={"custom": {"preferred_contact": "Email"}},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["custom"] == {"preferred_contact": "Email"}

    # a plain member sees another volunteer's name but not their custom values
    async with db_session() as session:
        other = await volunteers.create(session, None, "Other", "Person")
        await memberships.assign(
            session, None, other.id, seeded["team_id"], TeamRole.member
        )
        await custom_fields_service.set_values(
            session, None, other.id, {"preferred_contact": "Phone"}
        )
    r = await client.get(f"/api/volunteers/{other.id}", headers=member)
    assert r.status_code == 200
    assert r.json()["custom"] is None, "custom values are redacted like contact details"
    r = await client.get(f"/api/volunteers/{other.id}", headers=admin)
    assert r.json()["custom"] == {"preferred_contact": "Phone"}

    # member cannot edit/delete definitions
    r = await client.patch(
        f"/api/custom-fields/{field['id']}", json={"label": "X"}, headers=member
    )
    assert r.status_code == 403
    r = await client.delete(f"/api/custom-fields/{field['id']}", headers=admin)
    assert r.status_code == 204

    # scalar types validate through the same PATCH path
    r = await client.post(
        "/api/custom-fields",
        json={"label": "Years served", "field_type": "integer"},
        headers=admin,
    )
    assert r.status_code == 201
    r = await client.patch(
        f"/api/volunteers/{vid}",
        json={"custom": {"years_served": "three"}},
        headers=admin,
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/api/volunteers/{vid}", json={"custom": {"years_served": 3}}, headers=admin
    )
    assert r.status_code == 200
    assert r.json()["custom"]["years_served"] == 3


async def test_workload_flow(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")
    member = await _token(client, "member@example.org", "member-pass-phrase")

    # weight the team (team edits are admin-only across the board)
    r = await client.patch(
        f"/api/teams/{seeded['team_id']}", json={"workload_weight": 3}, headers=member
    )
    assert r.status_code == 403
    r = await client.patch(
        f"/api/teams/{seeded['team_id']}", json={"workload_weight": 3}, headers=admin
    )
    assert r.status_code == 200 and r.json()["workload_weight"] == 3.0

    # promote Maria to leader: score = 3 (weight) × 3 (leader multiplier) = 9 -> red
    r = await client.post(
        "/api/memberships",
        json={
            "volunteer_id": seeded["volunteer_id"],
            "team_id": seeded["team_id"],
            "role": "leader",
        },
        headers=admin,
    )
    assert r.status_code == 201

    r = await client.get("/api/workload/scores", headers=admin)
    assert r.status_code == 200
    (row,) = [s for s in r.json() if s["volunteer_id"] == seeded["volunteer_id"]]
    assert row["score"] == 9.0 and row["band"] == "red"

    # Maria leads the team now; the actor is rebuilt per request, so her
    # existing token immediately gains leader rights: she sees her people's
    # workload and may read the config
    r = await client.get("/api/workload/scores", headers=member)
    assert r.status_code == 200
    assert {s["volunteer_id"] for s in r.json()} == {seeded["volunteer_id"]}
    r = await client.get("/api/workload/config", headers=member)
    assert r.status_code == 200

    # config writes are admin-only, and invalid configs are rejected
    good = {
        "multipliers": {"leader": 2, "second": 2, "core": 1, "member": 1},
        "bands": [
            {"label": "ok", "color": "#4caf50", "upper": 6},
            {"label": "over", "color": "#e53935", "upper": None},
        ],
    }
    r = await client.put("/api/workload/config", json=good, headers=member)
    assert r.status_code == 403
    r = await client.put("/api/workload/config", json=good, headers=admin)
    assert r.status_code == 200

    bad = {**good, "bands": [{"label": "only", "color": "#000", "upper": 5}]}
    r = await client.put("/api/workload/config", json=bad, headers=admin)
    assert r.status_code == 422

    # new config applies: 3 × 2 = 6 -> "ok" band now
    r = await client.get("/api/workload/scores", headers=admin)
    (row,) = [s for s in r.json() if s["volunteer_id"] == seeded["volunteer_id"]]
    assert row["score"] == 6.0 and row["band"] == "ok"


async def test_the_api_will_not_change_your_own_address_behind_a_confirmation(
    client, seeded
):
    """`member@example.org` is Maria's own account, so her address is also her
    credential — and this API sends no email, so it cannot run the exchange
    that proves the new one. It says so instead of half-doing it. Everyone
    else's address stays an ordinary edit."""
    member = await _token(client, "member@example.org", "member-pass-phrase")
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")
    vid = seeded["volunteer_id"]

    r = await client.patch(
        f"/api/volunteers/{vid}",
        json={"email": "maria.new@example.org"},
        headers=member,
    )
    assert r.status_code == 422
    assert "/account" in r.json()["detail"], "and points at the page that can"

    # the same value is not a change, so it is not refused
    r = await client.patch(
        f"/api/volunteers/{vid}",
        json={"email": "Maria@Example.org", "phone": "555-0111"},
        headers=member,
    )
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == "555-0111"

    async with db_session() as session:
        assert (await volunteers.get(session, vid)).email == "maria@example.org"

    # an admin correcting somebody else's address is untouched by any of this
    r = await client.patch(
        f"/api/volunteers/{vid}",
        json={"email": "maria.fixed@example.org"},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["email"] == "maria.fixed@example.org"


async def test_a_pending_address_change_shows_on_your_own_account(client, seeded):
    member = await _token(client, "member@example.org", "member-pass-phrase")
    r = await client.get("/api/auth/me", headers=member)
    assert r.status_code == 200
    body = r.json()
    assert body["pending_email"] is None
    assert body["email_change_expires_at"] is None

    async with db_session() as session:
        account = await users.get_by_email(session, "member@example.org")
        await users.start_email_change(session, account.id, "later@example.org")

    r = await client.get("/api/auth/me", headers=member)
    assert r.json()["pending_email"] == "later@example.org"
    assert r.json()["email_change_expires_at"] is not None
    assert "email_change_token" not in r.json(), "the token itself never leaves"


async def test_roster_sheet_endpoint(client, seeded, token_admin, token_leader):
    """Repointing is admin-only — unlike the home doc, which is deliberately
    open to leaders and core members. The link is only recorded here; the
    nightly sync is what checks the file (jobs.drive_sync)."""
    team = seeded["team_id"]

    r = await client.get(f"/api/teams/{team}/roster-sheet", headers=token_leader)
    assert r.status_code == 200 and r.json() is None, "no sheet made yet"

    r = await client.patch(
        f"/api/teams/{team}/roster-sheet",
        json={"url": "https://docs.google.com/spreadsheets/d/abc123"},
        headers=token_leader,
    )
    assert r.status_code == 403, "a leader may not repoint their own team"

    r = await client.patch(
        f"/api/teams/{team}/roster-sheet",
        json={"url": "https://evil.test/spreadsheets/d/abc123"},
        headers=token_admin,
    )
    assert r.status_code == 422

    r = await client.patch(
        f"/api/teams/{team}/roster-sheet",
        json={
            "url": "https://docs.google.com/spreadsheets/d/abc123",
            "import_rows": True,
        },
        headers=token_admin,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requested_file_id"] == "abc123"
    assert body["requested_import"] is True
    assert body["requested_url"] == "https://docs.google.com/spreadsheets/d/abc123"
    assert body["file_id"] is None and body["url"] is None, (
        "the live pointer only moves once a sync has seen the file"
    )

    r = await client.patch(
        f"/api/teams/{team}/roster-sheet", json={"url": None}, headers=token_admin
    )
    assert r.status_code == 200 and r.json()["requested_file_id"] is None
