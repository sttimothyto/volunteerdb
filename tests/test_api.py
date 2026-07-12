"""JSON API smoke tests over ASGI: auth, CRUD, permissions, as-of."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from volunteerdb.api import api_router
from volunteerdb.api.deps import install_exception_handlers
from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers


@pytest.fixture
async def client(database):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(api_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded(database):
    async with db_session() as session:
        team = await teams.create(session, "Liturgy")
        v = await volunteers.create(session, "Maria", "Alvarez", "maria@example.org")
        await memberships.assign(session, v.id, team.id, TeamRole.member)
        await users.create(session, "admin@example.org", is_admin=True, password="secret-pw")
        await users.create(session, "member@example.org", volunteer_id=v.id, password="member-pw")
        return {"team_id": team.id, "volunteer_id": v.id}


async def _token(client, email, password) -> dict:
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_login_and_me(client, seeded):
    headers = await _token(client, "admin@example.org", "secret-pw")
    r = await client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["is_admin"] is True

    r = await client.post("/api/auth/login", json={"email": "admin@example.org", "password": "nope"})
    assert r.status_code == 401
    r = await client.get("/api/teams")
    assert r.status_code == 401


async def test_admin_crud_flow(client, seeded):
    headers = await _token(client, "admin@example.org", "secret-pw")

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
    headers = await _token(client, "member@example.org", "member-pw")

    r = await client.get("/api/teams", headers=headers)
    assert r.status_code == 200, "directory is browsable"

    r = await client.post(
        "/api/volunteers", json={"first_name": "X", "last_name": "Y"}, headers=headers
    )
    assert r.status_code == 403

    r = await client.post(
        "/api/memberships",
        json={"volunteer_id": seeded["volunteer_id"], "team_id": seeded["team_id"], "role": "leader"},
        headers=headers,
    )
    assert r.status_code == 403

    r = await client.get("/api/reports/coverage", headers=headers)
    assert r.status_code == 403

    # names visible, contacts redacted for a fellow member they can't view fully
    r = await client.get(f"/api/teams/{seeded['team_id']}/roster", headers=headers)
    assert r.status_code == 200


async def test_as_of_time_travel(client, seeded):
    headers = await _token(client, "admin@example.org", "secret-pw")

    await asyncio.sleep(0.02)
    before = datetime.now(UTC).isoformat()
    await asyncio.sleep(0.02)

    r = await client.patch(
        f"/api/volunteers/{seeded['volunteer_id']}", json={"first_name": "Renamed"}, headers=headers
    )
    assert r.status_code == 200

    r = await client.get(f"/api/volunteers/{seeded['volunteer_id']}", headers=headers)
    assert r.json()["first_name"] == "Renamed"

    r = await client.get(
        f"/api/volunteers/{seeded['volunteer_id']}", params={"as_of": before}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["first_name"] == "Maria", "as-of sees the pre-rename state"
