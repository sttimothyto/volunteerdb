"""Account management endpoints — all admin-only."""

from volunteerdb.db import db_session
from volunteerdb.services import volunteers

from tests.conftest import _token


async def test_users_endpoints_admin_only(client, seeded):
    member = await _token(client, "member@example.org", "member-pw")

    assert (await client.get("/api/users", headers=member)).status_code == 403
    r = await client.post("/api/users", json={"email": "x@example.org"}, headers=member)
    assert r.status_code == 403
    r = await client.patch("/api/users/1", json={"is_admin": True}, headers=member)
    assert r.status_code == 403
    assert (await client.post("/api/users/1/reinvite", headers=member)).status_code == 403
    assert (await client.post("/api/users/provision", headers=member)).status_code == 403


async def test_create_and_list_users(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pw")

    r = await client.post("/api/users", json={"email": "zed@example.org"}, headers=admin)
    assert r.status_code == 201
    passwordless = r.json()
    assert passwordless["has_password"] is False
    assert passwordless["invite_token"], "no password -> invite link instead"

    r = await client.post(
        "/api/users",
        json={"email": "with-pw@example.org", "password": "hunter2-long"},
        headers=admin,
    )
    assert r.status_code == 201
    assert r.json()["has_password"] is True
    assert r.json()["invite_token"] is None

    r = await client.get("/api/users", headers=admin)
    assert r.status_code == 200
    emails = [u["email"] for u in r.json()]
    assert emails == sorted(emails), "sorted by email"
    assert {"admin@example.org", "member@example.org", "zed@example.org"} <= set(emails)


async def test_patch_flags_deactivation_kills_token(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pw")

    r = await client.post(
        "/api/users", json={"email": "victim@example.org", "password": "victim-pw1"}, headers=admin
    )
    victim_id = r.json()["id"]
    victim = await _token(client, "victim@example.org", "victim-pw1")
    assert (await client.get("/api/auth/me", headers=victim)).status_code == 200

    r = await client.patch(f"/api/users/{victim_id}", json={"is_active": False}, headers=admin)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await client.get("/api/auth/me", headers=victim)).status_code == 401, (
        "deactivation takes effect on the victim's very next request"
    )

    r = await client.patch(
        f"/api/users/{victim_id}", json={"is_active": True, "is_admin": True}, headers=admin
    )
    assert r.status_code == 200
    assert (await client.get("/api/users", headers=victim)).status_code == 200, (
        "reactivated with admin rights, same token"
    )

    r = await client.patch("/api/users/999999", json={"is_admin": True}, headers=admin)
    assert r.status_code == 404


async def test_reinvite_resets_credentials(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pw")

    r = await client.post(
        "/api/users", json={"email": "lost@example.org", "password": "old-pass-1"}, headers=admin
    )
    user_id = r.json()["id"]
    assert (await _token(client, "lost@example.org", "old-pass-1"))["Authorization"]

    r = await client.post(f"/api/users/{user_id}/reinvite", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["has_password"] is False
    assert body["invite_token"], "fresh invite link to hand out"

    r = await client.post(
        "/api/auth/login", json={"email": "lost@example.org", "password": "old-pass-1"}
    )
    assert r.status_code == 401, "old password is dead"


async def test_provision_endpoint_reports_created_and_skipped(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pw")

    async with db_session() as session:
        await volunteers.create(session, "Ana", "Family", "family@example.org")
        shared = await volunteers.create(session, "Bob", "Family", "family@example.org")

    r = await client.post("/api/users/provision", headers=admin)
    assert r.status_code == 200
    report = r.json()

    created_emails = {u["email"] for u in report["created"]}
    # Maria (seeded, linked to member@example.org) is skipped; Ana gets the shared email
    assert created_emails == {"family@example.org"}
    assert all(u["invite_token"] for u in report["created"])

    skipped = {s["volunteer_id"]: s for s in report["skipped"]}
    assert skipped[shared.id]["reason"].startswith("email family@example.org already used")
    assert skipped[shared.id]["name"] == "Bob Family"
    assert skipped[seeded["volunteer_id"]]["reason"] == "already has an account"
