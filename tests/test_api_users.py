"""Account management endpoints — /api/users is admin-only throughout.

The one account-creating route that is not is POST /api/volunteers/{id}/invite,
covered at the bottom: it is scoped to one volunteer on the caller's own teams.
"""

import hashlib

import pytest

from volunteerdb.config import settings
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, users, volunteers

from tests.conftest import _token, db_session
from tests.fp_helpers import ok


async def test_users_endpoints_admin_only(client, seeded):
    member = await _token(client, "member@example.org", "member-pass-phrase")

    assert (await client.get("/api/users", headers=member)).status_code == 403
    r = await client.post("/api/users", json={"email": "x@example.org"}, headers=member)
    assert r.status_code == 403
    async with db_session() as session:
        admin_id = (await users.get_by_email(session, "admin@example.org")).id
    r = await client.patch(
        f"/api/users/{admin_id}", json={"is_admin": True}, headers=member
    )
    assert r.status_code == 403
    assert (
        await client.post(f"/api/users/{admin_id}/reinvite", headers=member)
    ).status_code == 403
    assert (
        await client.post("/api/users/provision", headers=member)
    ).status_code == 403


async def test_create_and_list_users(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post(
        "/api/users", json={"email": "zed@example.org"}, headers=admin
    )
    assert r.status_code == 201
    passwordless = r.json()
    assert passwordless["has_password"] is False
    assert passwordless["invite_token"], "no password -> invite link instead"
    assert passwordless["invite_expires_at"], "and the link says when it dies"

    r = await client.post(
        "/api/users",
        json={"email": "with-pw@example.org", "password": "hunter2-long-phrase"},
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


async def test_weak_password_is_refused_with_the_reason(client, seeded):
    """The policy is enforced in the service layer, so the API inherits it:
    WeakPassword is a ValueError and the installed handler makes that a 422
    carrying the sentence the caller needs (SP 800-63B §3.1.1.2 wants the
    reason for the rejection stated)."""
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post(
        "/api/users",
        json={"email": "weak@example.org", "password": "hunter2"},
        headers=admin,
    )
    assert r.status_code == 422
    assert "15 characters" in r.json()["detail"]

    r = await client.get("/api/users", headers=admin)
    assert "weak@example.org" not in [u["email"] for u in r.json()]


async def test_create_links_by_email_and_patch_relinks(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    async with db_session() as session:
        bruno = ok(
            await volunteers.create(
                session, None, "Bruno", "Cordeiro", "bc@example.org"
            )
        )
        pedro = ok(
            await volunteers.create(session, None, "Pedro", "Sousa", "ps@example.org")
        )

    r = await client.post("/api/users", json={"email": "bc@example.org"}, headers=admin)
    assert r.status_code == 201
    user_id = r.json()["id"]
    assert r.json()["volunteer_id"] == bruno.id

    r = await client.patch(
        f"/api/users/{user_id}", json={"volunteer_id": pedro.id}, headers=admin
    )
    assert r.status_code == 200 and r.json()["volunteer_id"] == pedro.id

    r = await client.patch(
        f"/api/users/{user_id}", json={"is_admin": True}, headers=admin
    )
    assert r.json()["volunteer_id"] == pedro.id, "omitting the field leaves it alone"

    r = await client.patch(
        f"/api/users/{user_id}", json={"volunteer_id": None}, headers=admin
    )
    assert r.json()["volunteer_id"] is None, "explicit null unlinks"

    r = await client.patch(
        f"/api/users/{user_id}", json={"volunteer_id": 424242}, headers=admin
    )
    assert r.status_code == 404


async def test_provision_endpoint_adopts_an_unlinked_account(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post(
        "/api/users", json={"email": "late@example.org"}, headers=admin
    )
    assert r.json()["volunteer_id"] is None, "nobody holds that address yet"
    async with db_session() as session:
        late = ok(
            await volunteers.create(
                session, None, "Late", "Arrival", "late@example.org"
            )
        )

    r = await client.post("/api/users/provision", headers=admin)
    assert r.status_code == 200
    linked = r.json()["linked"]
    assert [u["email"] for u in linked] == ["late@example.org"]
    assert [u["volunteer_id"] for u in linked] == [late.id]


async def test_patch_flags_deactivation_kills_token(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post(
        "/api/users",
        json={"email": "victim@example.org", "password": "victim-pass-phrase"},
        headers=admin,
    )
    victim_id = r.json()["id"]
    victim = await _token(client, "victim@example.org", "victim-pass-phrase")
    assert (await client.get("/api/auth/me", headers=victim)).status_code == 200

    r = await client.patch(
        f"/api/users/{victim_id}", json={"is_active": False}, headers=admin
    )
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await client.get("/api/auth/me", headers=victim)).status_code == 401, (
        "deactivation takes effect on the victim's very next request"
    )

    r = await client.patch(
        f"/api/users/{victim_id}",
        json={"is_active": True, "is_admin": True},
        headers=admin,
    )
    assert r.status_code == 200
    assert (await client.get("/api/users", headers=victim)).status_code == 200, (
        "reactivated with admin rights, same token"
    )

    r = await client.patch("/api/users/999999", json={"is_admin": True}, headers=admin)
    assert r.status_code == 404


async def test_reinvite_resets_credentials(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    r = await client.post(
        "/api/users",
        json={"email": "lost@example.org", "password": "old-pass-phrase-1"},
        headers=admin,
    )
    user_id = r.json()["id"]
    assert (await _token(client, "lost@example.org", "old-pass-phrase-1"))[
        "Authorization"
    ]

    r = await client.post(f"/api/users/{user_id}/reinvite", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["has_password"] is False
    assert body["invite_token"], "fresh invite link to hand out"

    r = await client.post(
        "/api/auth/login",
        json={"email": "lost@example.org", "password": "old-pass-phrase-1"},
    )
    assert r.status_code == 401, "old password is dead"


async def test_provision_endpoint_reports_created_and_skipped(client, seeded):
    admin = await _token(client, "admin@example.org", "secret-pass-phrase")

    async with db_session() as session:
        ok(
            await volunteers.create(
                session, None, "Ana", "Family", "family@example.org"
            )
        )
        shared = ok(
            await volunteers.create(
                session, None, "Bob", "Family", "family@example.org"
            )
        )

    r = await client.post("/api/users/provision", headers=admin)
    assert r.status_code == 200
    report = r.json()

    created_emails = {u["email"] for u in report["created"]}
    # Maria (seeded, linked to member@example.org) is skipped; Ana gets the shared email
    assert created_emails == {"family@example.org"}
    assert all(u["invite_token"] for u in report["created"])

    skipped = {s["volunteer_id"]: s for s in report["skipped"]}
    assert skipped[shared.id]["reason"].startswith(
        "email family@example.org already used"
    )
    assert skipped[shared.id]["name"] == "Bob Family"
    assert skipped[seeded["volunteer_id"]]["reason"] == "already has an account"


@pytest.fixture
async def rostered(seeded):
    """Someone on the seeded Liturgy team with an email and no account —
    exactly what a leader finds on their roster and wants to fix."""
    async with db_session() as session:
        nils = ok(
            await volunteers.create(session, None, "Nils", "Nobody", "nils@example.org")
        )
        ok(
            await memberships.assign(
                session, None, nils.id, seeded["team_id"], TeamRole.member
            )
        )
        return nils.id


async def test_volunteer_invite_is_open_to_leaders_and_core(
    client, seeded, rostered, token_leader, token_core, clock
):
    r = await client.post(f"/api/volunteers/{rostered}/invite", headers=token_leader)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "nils@example.org"
    assert body["volunteer_id"] == rostered, "linked to the person invited"
    assert body["invite_expires_at"], "the caller learns a link is out, and until when"
    assert body["has_password"] is False
    assert body["is_admin"] is False, "a leader can never mint an admin"

    async with db_session() as session:
        account = await users.account_for_volunteer(session, rostered)
        first_token = account.invite_token
        assert first_token, "the invite really is armed (as a digest)"
        # core members hold the same right; the account exists now, so this
        # exercises the re-arm path rather than creation
    # a lapsed invite is a PAST expiry, not a missing one: the pair is set and
    # cleared together (ck_app_user_invite_pair), so time is what lapses it
    clock.advance(hours=settings().invite_ttl_hours + 1)
    again = await client.post(f"/api/volunteers/{rostered}/invite", headers=token_core)
    assert again.status_code == 200, again.text
    async with db_session() as session:
        account = await users.account_for_volunteer(session, rostered)
        assert account.invite_token != first_token, "a fresh token"


async def test_volunteer_invite_refuses_a_plain_member(
    client, seeded, rostered, token_member
):
    r = await client.post(f"/api/volunteers/{rostered}/invite", headers=token_member)
    assert r.status_code == 403
    async with db_session() as session:
        assert await users.account_for_volunteer(session, rostered) is None


async def test_volunteer_invite_maps_service_refusals(client, seeded, token_admin):
    """Admins reach anyone, so 404 is visible here; to anyone else an unknown id
    and another ministry's volunteer both answer 403, which is the point."""
    r = await client.post("/api/volunteers/424242/invite", headers=token_admin)
    assert r.status_code == 404

    async with db_session() as session:
        quiet = ok(await volunteers.create(session, None, "Hank", "Host"))  # no email
    r = await client.post(f"/api/volunteers/{quiet.id}/invite", headers=token_admin)
    assert r.status_code == 422
    assert "no email address" in r.json()["detail"]

    # the seeded member already has a working, password-bearing account
    r = await client.post(
        f"/api/volunteers/{seeded['volunteer_id']}/invite", headers=token_admin
    )
    assert r.status_code == 422
    assert "already has a working account" in r.json()["detail"]


@pytest.fixture
def sent_api(env) -> list[tuple[str, str, str]]:
    """What the API mailed: the Env's recording mailer, which the api_app
    fixture hands the routes (they send through ctx.env.mailer)."""
    return env.mailer.sent


async def test_only_an_admin_is_handed_the_invite_link(
    client, seeded, rostered, token_leader, token_admin, sent_api, clock
):
    """The link signs you in as that volunteer, and a leader may add anybody to
    their own team and then edit their address — so handing them the token made
    every never-signed-in account takeable. Non-admins get delivery to the
    address on file instead; this is the one route that mails, because the
    alternative is minting a credential that reaches nobody.

    Admins keep both the token and the no-mail rule: they hand it over
    themselves, which is what the GUI's visible link has always been for."""
    r = await client.post(f"/api/volunteers/{rostered}/invite", headers=token_leader)
    assert r.status_code == 200, r.text
    assert r.json()["invite_token"] is None, "never to a non-admin"

    assert len(sent_api) == 1, "mailed instead, so the invite still arrives"
    to, _subject, body = sent_api[0]
    assert to == "nils@example.org", "to the address on the volunteer's own record"
    mailed = body.split("/invite/", 1)[1].split()[0].rstrip(".,)")

    async with db_session() as session:
        account = await users.account_for_volunteer(session, rostered)
        # the stored value is a DIGEST of the mailed link, never the link
        # (services.users._issue_invite): a read of this table hands out nothing
        assert account.invite_token == hashlib.sha256(mailed.encode()).hexdigest()
        assert account.invite_token != mailed

    # a second, admin-issued invite: token back, and still no mail from the API
    clock.advance(hours=settings().invite_ttl_hours + 1)
    sent_api.clear()
    r = await client.post(f"/api/volunteers/{rostered}/invite", headers=token_admin)
    assert r.status_code == 200, r.text
    assert r.json()["invite_token"], "an admin hands the link over in person"
    assert sent_api == [], "and no mail from the API, as everywhere else"
