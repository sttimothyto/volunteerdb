"""API plumbing: Bearer auth, token lifecycle, throttling, error mapping, deletes."""

import httpx
import pytest

from volunteerdb import throttle
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, volunteers

from tests.conftest import _token, api_client_for, db_session
from tests.fakes import FailingMailer, FakeHttp
from tests.fp_helpers import ok


async def test_missing_and_malformed_bearer_401(client, seeded):
    r = await client.get("/api/teams")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"

    for bad in ("Basic dXNlcjpwdw==", "Bearer", "Bearer   ", "Bearer not-a-real-token"):
        r = await client.get("/api/teams", headers={"Authorization": bad})
        assert r.status_code == 401, bad
        assert r.headers["WWW-Authenticate"] == "Bearer"


async def test_relogin_revokes_previous_token(client, seeded):
    first = await _token(client, "admin@example.org", "secret-pass-phrase")
    second = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.get("/api/auth/me", headers=first)
    assert r.status_code == 401, "each login revokes the previous token"
    r = await client.get("/api/auth/me", headers=second)
    assert r.status_code == 200


async def test_login_throttle_429(client, seeded):
    for _ in range(throttle.LIMITS["pw"].hits):
        r = await client.post(
            "/api/auth/login", json={"email": "admin@example.org", "password": "wrong"}
        )
        assert r.status_code == 401

    r = await client.post(
        "/api/auth/login",
        json={"email": "admin@example.org", "password": "secret-pass-phrase"},
    )
    assert r.status_code == 429, "throttled even with the correct password"

    r = await client.post(
        "/api/auth/login", json={"email": "member@example.org", "password": "wrong"}
    )
    assert r.status_code == 401, "another email on the same IP is not blocked yet"


async def test_error_mapping_404_409_422(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.get("/api/teams/999999", headers=admin)
    assert r.status_code == 404

    r = await client.post(
        "/api/memberships",
        json={"volunteer_id": 999999, "team_id": seeded["team_id"], "role": "member"},
        headers=admin,
    )
    assert r.status_code == 409, "FK violation maps to conflict"

    r = await client.patch(
        f"/api/teams/{seeded['team_id']}", json={"workload_weight": -2}, headers=admin
    )
    assert r.status_code == 422, "negative weight is a validation error"

    r = await client.post(
        "/api/teams", json={"name": "Negative", "workload_weight": -2}, headers=admin
    )
    assert r.status_code == 422, "POST must refuse what PATCH refuses"


async def test_new_teams_start_at_weight_one(client, seeded):
    """A new ministry is ordinary work, not zero work. Omitting the weight
    starts it at 1; an explicit null is still how a team is excluded, because
    0 has always been what "unweighted" means (models.Team.workload_weight)."""
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post("/api/teams", json={"name": "Sacristans"}, headers=admin)
    assert r.status_code == 201
    assert r.json()["workload_weight"] == 1.0, "an omitted weight means 1"

    r = await client.post(
        "/api/teams", json={"name": "Retired", "workload_weight": None}, headers=admin
    )
    assert r.status_code == 201
    assert r.json()["workload_weight"] == 0, "an explicit null still means excluded"

    r = await client.post(
        "/api/teams", json={"name": "Heavy", "workload_weight": 3}, headers=admin
    )
    assert r.json()["workload_weight"] == 3.0, "an explicit weight still wins"


async def test_team_cycle_maps_to_422(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post("/api/teams", json={"name": "Parent"}, headers=admin)
    parent_id = r.json()["id"]
    r = await client.post(
        "/api/teams", json={"name": "Child", "parent_team_id": parent_id}, headers=admin
    )
    child_id = r.json()["id"]

    r = await client.patch(
        f"/api/teams/{parent_id}", json={"parent_team_id": child_id}, headers=admin
    )
    assert r.status_code == 422
    assert "ancestor" in r.json()["detail"]


async def test_team_patch_clear_parent_and_clear_weight(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post("/api/teams", json={"name": "Top"}, headers=admin)
    top_id = r.json()["id"]
    r = await client.post(
        "/api/teams",
        json={"name": "Sub", "parent_team_id": top_id, "workload_weight": 2.5},
        headers=admin,
    )
    sub_id = r.json()["id"]

    r = await client.patch(
        f"/api/teams/{sub_id}", json={"clear_parent": True}, headers=admin
    )
    assert r.status_code == 200
    body = r.json()
    assert body["parent_team_id"] is None
    assert body["workload_weight"] == 2.5, "weight untouched by clear_parent"

    r = await client.patch(
        f"/api/teams/{sub_id}", json={"clear_workload_weight": True}, headers=admin
    )
    assert r.status_code == 200
    body = r.json()
    # clearing puts the weight back to 0, and 0 IS unweighted: the column has no
    # NULL any more, because NULL and 0 always scored identically
    assert body["workload_weight"] == 0
    assert body["name"] == "Sub", "omitted fields untouched"


async def test_delete_endpoints_admin_only_then_404(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")
    member = await _token(client, "member@example.org", "member-pass-phrase")

    # a disposable volunteer with a membership on the seeded team
    r = await client.post(
        "/api/volunteers",
        json={"first_name": "Dis", "last_name": "Posable"},
        headers=admin,
    )
    vid = r.json()["id"]
    r = await client.post(
        "/api/memberships",
        json={"volunteer_id": vid, "team_id": seeded["team_id"], "role": "member"},
        headers=admin,
    )
    mid = r.json()["id"]

    r = await client.delete(f"/api/memberships/{mid}", headers=member)
    assert r.status_code == 403
    r = await client.delete(f"/api/memberships/{mid}", headers=admin)
    assert r.status_code == 204
    r = await client.delete(f"/api/memberships/{mid}", headers=admin)
    assert r.status_code == 404

    r = await client.delete(f"/api/volunteers/{vid}", headers=member)
    assert r.status_code == 403
    r = await client.delete(f"/api/volunteers/{vid}", headers=admin)
    assert r.status_code == 204
    r = await client.get(f"/api/volunteers/{vid}", headers=admin)
    assert r.status_code == 404

    r = await client.post("/api/teams", json={"name": "Doomed"}, headers=admin)
    tid = r.json()["id"]
    r = await client.delete(f"/api/teams/{tid}", headers=member)
    assert r.status_code == 403
    r = await client.delete(f"/api/teams/{tid}", headers=admin)
    assert r.status_code == 204
    r = await client.get(f"/api/teams/{tid}", headers=admin)
    assert r.status_code == 404


def _google_down(env):
    """An Env whose parish Google grant is set and whose every request to
    Google fails with a 500: what the sync and the refetch meet on a bad day."""
    configured = env.settings.model_copy(
        update={
            "sheets_client_id": "cid",
            "sheets_client_secret": "secret",
            "sheets_refresh_token": "refresh",
            "sheets_folder_id": "folder",
        }
    )
    return env.with_(
        settings=configured,
        http=FakeHttp(lambda r: httpx.Response(500, json={"error": "down"})),
    )


async def test_a_failing_google_is_recorded_against_the_sheet_not_a_502(seeded, env):
    """The sync route folds the External into the team's sync record: a 422
    carrying the leg that failed, and the sheet row marked, which is what the
    team page shows a leader. A 502 would be true and useless."""
    async with api_client_for(_google_down(env)) as client:
        admin = await _token(client, "admin@example.org", "secret-pass-phrase")
        team_id = seeded["team_id"]
        r = await client.patch(
            f"/api/teams/{team_id}/roster-sheet",
            json={"url": "https://docs.google.com/spreadsheets/d/abc123/edit"},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        r = await client.post(
            f"/api/teams/{team_id}/roster-sheet/sync",
            json={"direction": "export"},
            headers=admin,
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "token mint failed: HTTP 500"
        r = await client.get(f"/api/teams/{team_id}/roster-sheet", headers=admin)
        assert r.json()["last_status"] == "error"
        assert r.json()["last_error"] == "token mint failed: HTTP 500"


async def test_a_failing_google_keeps_the_last_page_and_says_so(seeded, env):
    """A refetch that fails reports itself in the page's status rather than
    blanking what the world can see -- and rather than a 502."""
    async with api_client_for(_google_down(env)) as client:
        admin = await _token(client, "admin@example.org", "secret-pass-phrase")
        team_id = seeded["team_id"]
        r = await client.patch(
            f"/api/teams/{team_id}/home-doc",
            json={"url": "https://docs.google.com/document/d/doc1"},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        r = await client.post(f"/api/teams/{team_id}/page/fetch", headers=admin)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "error"
        assert "500" in r.json()["error"]


@pytest.mark.parametrize(
    ("who", "method", "path", "body", "status"),
    [
        # a leader's invite is mailed (an admin's is handed over in person)
        (
            "lena@example.org",
            "POST",
            "/api/volunteers/{volunteer_id}/invite",
            None,
            200,
        ),
        (
            "admin@example.org",
            "POST",
            "/api/auth/email-change",
            {"new_email": "moved@example.org"},
            202,
        ),
        (
            "admin@example.org",
            "PUT",
            "/api/auth/password",
            {
                "current_password": "secret-pass-phrase",
                "new_password": "cedar lamp figs",
            },
            204,
        ),
    ],
)
async def test_a_mailer_that_fails_never_fails_the_request(
    seeded, token_leader, env, who, method, path, body, status
):
    """Mail never rides a transaction: the account, the address change or the
    new password committed, and a message that could not be sent is a count
    in the effect report, not an error to the caller."""
    async with db_session() as session:  # somebody with no account yet
        fresh = ok(
            await volunteers.create(session, None, "Fresh", "Face", "fresh@example.org")
        )
        ok(
            await memberships.assign(
                session, None, fresh.id, seeded["team_id"], TeamRole.member
            )
        )
    passwords = {
        "lena@example.org": "leader-pass-phrase",
        "admin@example.org": "secret-pass-phrase",
    }
    failing = FailingMailer()
    async with api_client_for(env.with_(mailer=failing)) as client:
        headers = await _token(client, who, passwords[who])
        url = path.format(volunteer_id=fresh.id)
        r = await client.request(method, url, json=body, headers=headers)
        assert r.status_code == status, r.text
    assert failing.sent, "the send was attempted, and reported failed"
