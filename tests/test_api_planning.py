"""Planning API: propose/accept/decline/withdraw with role-scoped authz."""

from volunteerdb.db import db_session
from volunteerdb.services import volunteers


async def test_planning_flow(client, seeded, token_admin, token_member, token_leader):
    team_id, maria_id = seeded["team_id"], seeded["volunteer_id"]

    # a plain member has no planning access at all
    r = await client.get("/api/planning/proposals", headers=token_member)
    assert r.status_code == 403
    body = {"team_id": team_id, "volunteer_id": maria_id, "role": "second"}
    r = await client.post("/api/planning/proposals", json=body, headers=token_member)
    assert r.status_code == 403

    # the Liturgy leader proposes Maria as second-in-command
    r = await client.post("/api/planning/proposals", json=body, headers=token_leader)
    assert r.status_code == 201, r.text
    proposal = r.json()
    assert proposal["status"] == "proposed"
    assert proposal["role_label"] == "Second-in-command"

    # duplicate open proposal, even from someone else -> conflict
    r = await client.post("/api/planning/proposals", json=body, headers=token_admin)
    assert r.status_code == 409

    # the leader sees it in their scoped list
    r = await client.get("/api/planning/proposals", headers=token_leader)
    assert [p["id"] for p in r.json()] == [proposal["id"]]

    # accepting creates the membership
    r = await client.post(f"/api/planning/proposals/{proposal['id']}/accept", headers=token_leader)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    r = await client.get(f"/api/teams/{team_id}/roster", headers=token_admin)
    roles = {e["volunteer"]["id"]: e["role"] for e in r.json()}
    assert roles[maria_id] == "second"

    # deciding twice is rejected
    r = await client.post(f"/api/planning/proposals/{proposal['id']}/decline", headers=token_admin)
    assert r.status_code == 422

    # unknown proposal -> 404
    r = await client.post("/api/planning/proposals/424242/accept", headers=token_admin)
    assert r.status_code == 404


async def test_planning_withdraw_rights(client, seeded, token_admin, token_member, token_leader):
    async with db_session() as session:
        walter = await volunteers.create(session, "Walter", "Willing")
        walter_id = walter.id

    body = {"team_id": seeded["team_id"], "volunteer_id": walter_id, "role": "leader"}
    r = await client.post("/api/planning/proposals", json=body, headers=token_leader)
    assert r.status_code == 201
    pid = r.json()["id"]

    # a plain member can withdraw nothing
    r = await client.post(f"/api/planning/proposals/{pid}/withdraw", headers=token_member)
    assert r.status_code == 403

    # the proposer may withdraw their own proposal
    r = await client.post(f"/api/planning/proposals/{pid}/withdraw", headers=token_leader)
    assert r.status_code == 200
    assert r.json()["status"] == "withdrawn"
