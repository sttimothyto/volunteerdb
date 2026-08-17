"""Elections API: the nomination + STAR-voting pipeline with role-scoped authz.

Routes derive phase from local_today(), so tests steer a proposal through
its phases by PATCHing deadlines relative to today (the API cannot inject
`today` — that seam is service-level only).
"""

from datetime import timedelta

from volunteerdb.db import db_session
from volunteerdb.services import elections, volunteers


def _days(n: int) -> str:
    return (elections.local_today() + timedelta(days=n)).isoformat()


async def _walter_id() -> int:
    async with db_session() as session:
        walter = await volunteers.create(session, "Walter", "Willing")
        return walter.id


async def test_elections_flow_and_ballot_secrecy(
    client, seeded, token_admin, token_member, token_leader
):
    team_id, maria_id = seeded["team_id"], seeded["volunteer_id"]
    walter_id = await _walter_id()

    # a plain member (no roll, no team to manage) has no elections access
    r = await client.get("/api/elections/proposals", headers=token_member)
    assert r.status_code == 403
    body = {
        "team_id": team_id,
        "role": "second",
        "nomination_deadline": _days(3),
        "voting_deadline": _days(10),
        "candidates": [{"volunteer_id": walter_id, "note": "willing and able"}],
    }
    r = await client.post("/api/elections/proposals", json=body, headers=token_member)
    assert r.status_code == 403

    # the Liturgy leader opens the proposal
    r = await client.post("/api/elections/proposals", json=body, headers=token_leader)
    assert r.status_code == 201, r.text
    proposal = r.json()
    pid = proposal["id"]
    assert proposal["status"] == "open"
    assert proposal["phase"] == "nominating"
    assert proposal["role_label"] == "Second-in-command"

    # one open proposal per seat, even for an admin
    r = await client.post("/api/elections/proposals", json=body, headers=token_admin)
    assert r.status_code == 409

    # detail: candidate carries name + current commitments; roll = Lena (leader)
    r = await client.get(f"/api/elections/proposals/{pid}", headers=token_leader)
    assert r.status_code == 200
    detail = r.json()
    assert detail["path"] == "Liturgy"
    assert detail["candidates"][0]["volunteer_name"] == "Walter Willing"
    assert detail["candidates"][0]["assignments"] == [], "Walter is uncommitted"
    assert [v["volunteer_name"] for v in detail["voters"]] == ["Lena Leader"]
    r = await client.get(f"/api/elections/proposals/{pid}", headers=token_member)
    assert r.status_code == 403, "not on the roll, not a manager"

    # only managers edit the roll; Maria joins as a voting member
    r = await client.post(
        f"/api/elections/proposals/{pid}/voters",
        json={"volunteer_id": maria_id},
        headers=token_member,
    )
    assert r.status_code == 403
    r = await client.post(
        f"/api/elections/proposals/{pid}/voters",
        json={"volunteer_id": maria_id},
        headers=token_leader,
    )
    assert r.status_code == 201
    assert r.json()["volunteer_name"] == "Maria Alvarez"

    # a voter may see and nominate, but not vote before the voting phase
    r = await client.get("/api/elections/proposals", headers=token_member)
    assert r.status_code == 200 and [p["id"] for p in r.json()] == [pid]
    r = await client.put(
        f"/api/elections/proposals/{pid}/ballot",
        json={"scores": {}},
        headers=token_member,
    )
    assert r.status_code == 422, "still nominating"

    # nominations close (deadline moved into the past) -> voting opens
    r = await client.patch(
        f"/api/elections/proposals/{pid}",
        json={"nomination_deadline": _days(-1)},
        headers=token_member,
    )
    assert r.status_code == 403
    r = await client.patch(
        f"/api/elections/proposals/{pid}",
        json={"nomination_deadline": _days(-1)},
        headers=token_leader,
    )
    assert r.status_code == 200 and r.json()["phase"] == "voting"

    walter_cand = detail["candidates"][0]["id"]
    r = await client.put(
        f"/api/elections/proposals/{pid}/ballot",
        json={"scores": {str(walter_cand): 6}},
        headers=token_member,
    )
    assert r.status_code == 422, "scores are 0-5"
    r = await client.put(
        f"/api/elections/proposals/{pid}/ballot",
        json={"scores": {"424242": 3}},
        headers=token_member,
    )
    assert r.status_code == 422, "unknown candidate"
    r = await client.put(
        f"/api/elections/proposals/{pid}/ballot",
        json={"scores": {str(walter_cand): 4}},
        headers=token_member,
    )
    assert r.status_code == 204
    r = await client.put(
        f"/api/elections/proposals/{pid}/ballot",
        json={"scores": {str(walter_cand): 2}},
        headers=token_leader,
    )
    assert r.status_code == 204

    # ballot secrecy: turnout only, no scores, no tally while voting is open
    r = await client.get(f"/api/elections/proposals/{pid}", headers=token_leader)
    detail = r.json()
    assert detail["tally"] is None
    assert all(v["has_voted"] for v in detail["voters"])
    assert set(detail["voters"][0]) == {
        "id",
        "volunteer_id",
        "volunteer_name",
        "has_account",
        "has_voted",
    }, "a voter row must never carry scores"
    assert '"score"' not in r.text
    r = await client.get(f"/api/elections/proposals/{pid}/tally", headers=token_leader)
    assert r.status_code == 422

    # voting closes -> the tally becomes visible to roll and managers
    r = await client.patch(
        f"/api/elections/proposals/{pid}",
        json={"nomination_deadline": _days(-3), "voting_deadline": _days(-1)},
        headers=token_leader,
    )
    assert r.status_code == 200 and r.json()["phase"] == "concluded"
    r = await client.get(f"/api/elections/proposals/{pid}/tally", headers=token_member)
    assert r.status_code == 200
    tally = r.json()
    assert tally["ballot_count"] == 2
    assert tally["totals"] == [
        {"candidate_id": walter_cand, "volunteer_name": "Walter Willing", "total": 6}
    ]
    assert tally["winner_candidate_id"] == walter_cand
    assert not tally["tie"]

    # appointing is a manager's act and creates the membership
    r = await client.post(
        f"/api/elections/proposals/{pid}/appoint",
        json={"candidate_id": walter_cand},
        headers=token_member,
    )
    assert r.status_code == 403
    r = await client.post(
        f"/api/elections/proposals/{pid}/appoint",
        json={"candidate_id": walter_cand},
        headers=token_leader,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "appointed"
    assert r.json()["appointed_candidate_id"] == walter_cand
    r = await client.get(f"/api/teams/{team_id}/roster", headers=token_admin)
    roles = {e["volunteer"]["id"]: e["role"] for e in r.json()}
    assert roles[walter_id] == "second"

    # deciding twice is rejected; unknown ids 404
    r = await client.post(
        f"/api/elections/proposals/{pid}/appoint",
        json={"candidate_id": walter_cand},
        headers=token_leader,
    )
    assert r.status_code == 422
    r = await client.post("/api/elections/proposals/424242/cancel", headers=token_admin)
    assert r.status_code == 404


async def test_cancel_and_new_round(client, seeded, token_leader):
    team_id = seeded["team_id"]
    walter_id = await _walter_id()
    body = {
        "team_id": team_id,
        "role": "leader",
        "nomination_deadline": _days(2),
        "voting_deadline": _days(9),
        "candidates": [{"volunteer_id": walter_id, "note": "a shepherd"}],
    }
    r = await client.post("/api/elections/proposals", json=body, headers=token_leader)
    pid = r.json()["id"]
    r = await client.post(
        f"/api/elections/proposals/{pid}/cancel", headers=token_leader
    )
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.post(
        f"/api/elections/proposals/{pid}/cancel", headers=token_leader
    )
    assert r.status_code == 422

    # a fresh seat run: conclude it untouched, then repeat with new deadlines
    r = await client.post("/api/elections/proposals", json=body, headers=token_leader)
    pid = r.json()["id"]
    r = await client.patch(
        f"/api/elections/proposals/{pid}",
        json={"nomination_deadline": _days(-3), "voting_deadline": _days(-1)},
        headers=token_leader,
    )
    assert r.status_code == 200
    r = await client.post(
        f"/api/elections/proposals/{pid}/new-round",
        json={"nomination_deadline": _days(4), "voting_deadline": _days(11)},
        headers=token_leader,
    )
    assert r.status_code == 201, r.text
    fresh = r.json()
    assert fresh["id"] != pid and fresh["phase"] == "nominating"
    r = await client.get(
        f"/api/elections/proposals/{fresh['id']}", headers=token_leader
    )
    detail = r.json()
    assert detail["candidates"][0]["note"] == "a shepherd", "candidates travel"
    assert not any(v["has_voted"] for v in detail["voters"]), "ballots do not"
    r = await client.get(f"/api/elections/proposals/{pid}", headers=token_leader)
    assert r.json()["proposal"]["status"] == "cancelled"


async def test_volunteer_proposals_involvement(
    client, seeded, token_admin, token_member, token_leader
):
    """GET /volunteers/{id}/proposals: flags per kind of involvement, access
    gate and scoping borrowed from the elections list."""
    team_id, maria_id = seeded["team_id"], seeded["volunteer_id"]
    walter_id = await _walter_id()
    r = await client.post(
        "/api/elections/proposals",
        json={
            "team_id": team_id,
            "role": "second",
            "nomination_deadline": _days(3),
            "voting_deadline": _days(10),
            "candidates": [{"volunteer_id": walter_id, "note": "willing and able"}],
        },
        headers=token_leader,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    # Walter: candidate only
    r = await client.get(f"/api/volunteers/{walter_id}/proposals", headers=token_leader)
    assert r.status_code == 200
    [row] = r.json()
    assert row["proposal"]["id"] == pid and row["path"] == "Liturgy"
    assert row["as_candidate"] and not row["as_voter"] and not row["appointed"]
    assert row["proposal"]["phase"] == "nominating"

    # a plain member has no elections access at all
    r = await client.get(f"/api/volunteers/{walter_id}/proposals", headers=token_member)
    assert r.status_code == 403

    # an uninvolved volunteer is simply an empty list
    r = await client.get(f"/api/volunteers/{maria_id}/proposals", headers=token_admin)
    assert r.status_code == 200 and r.json() == []

    # once Maria joins the roll she gains access and appears as a voter
    r = await client.post(
        f"/api/elections/proposals/{pid}/voters",
        json={"volunteer_id": maria_id},
        headers=token_leader,
    )
    assert r.status_code == 201
    r = await client.get(f"/api/volunteers/{maria_id}/proposals", headers=token_member)
    assert r.status_code == 200
    [row] = r.json()
    assert row["as_voter"] and not row["as_candidate"]

    # conclude voting by PATCHing the deadlines into the past, then appoint:
    # the winner's row flips to appointed with the phase gone
    r = await client.patch(
        f"/api/elections/proposals/{pid}",
        json={"nomination_deadline": _days(-10), "voting_deadline": _days(-5)},
        headers=token_leader,
    )
    assert r.status_code == 200, r.text
    r = await client.get(f"/api/elections/proposals/{pid}", headers=token_leader)
    cand_id = r.json()["candidates"][0]["id"]
    r = await client.post(
        f"/api/elections/proposals/{pid}/appoint",
        json={"candidate_id": cand_id},
        headers=token_leader,
    )
    assert r.status_code == 200, r.text
    r = await client.get(f"/api/volunteers/{walter_id}/proposals", headers=token_leader)
    [row] = r.json()
    assert row["appointed"] and row["proposal"]["status"] == "appointed"
    assert row["proposal"]["phase"] is None

    # an unknown volunteer id is an empty list too, like /timeline
    r = await client.get("/api/volunteers/999999/proposals", headers=token_admin)
    assert r.status_code == 200 and r.json() == []
