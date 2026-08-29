"""The caller's own account, over JSON — the /account page's counterpart.

Until these endpoints existed the API could *read* that an address change was
pending (GET /auth/me) but never drive one, and could not set, change or remove
a password at all: an API client's account was managed entirely from a browser.

Two things stay deliberately absent, and the tests at the bottom pin them:
emailed-code sign-in (an API token is tied to a password) and changing somebody
else's address through here.
"""

import hashlib

import pytest

from volunteerdb.services import users

from tests.conftest import _token, db_session


@pytest.fixture
def sent(env) -> list[tuple[str, str, str]]:
    """What the API mailed: the Env's recording mailer (the routes send
    through ctx.env.mailer, not the mail.send_email shim)."""
    return env.mailer.sent


async def test_changing_your_own_password_needs_the_current_one(client, seeded, sent):
    """Always the current one, unlike the GUI: a token is only ever issued
    against a password, so a caller holding one can produce it."""
    member = await _token(client, "member@example.org", "member-pass-phrase")

    r = await client.put(
        "/api/auth/password",
        json={
            "current_password": "wrong-pass-phrase",
            "new_password": "cedar lamp figs",
        },
        headers=member,
    )
    assert r.status_code == 403
    assert sent == [], "a refused change tells nobody"

    r = await client.put(
        "/api/auth/password",
        json={
            "current_password": "member-pass-phrase",
            "new_password": "cedar lamp figs",
        },
        headers=member,
    )
    assert r.status_code == 204
    assert await _token(client, "member@example.org", "cedar lamp figs")
    assert any("was just changed" in body for _, _, body in sent), (
        "the account's address hears about it, on a channel the caller does not control"
    )


async def test_a_weak_new_password_is_refused_by_the_policy(client, seeded):
    member = await _token(client, "member@example.org", "member-pass-phrase")
    r = await client.put(
        "/api/auth/password",
        json={"current_password": "member-pass-phrase", "new_password": "short"},
        headers=member,
    )
    assert r.status_code == 422
    assert "too short" in r.json()["detail"]


async def test_removing_your_password_revokes_the_token_that_asked(
    client, seeded, sent
):
    """The token was issued against the password being removed, so this is the
    last request it serves — the same rule /account follows."""
    member = await _token(client, "member@example.org", "member-pass-phrase")
    assert (
        await client.delete("/api/auth/password", headers=member)
    ).status_code == 204

    r = await client.get("/api/auth/me", headers=member)
    assert r.status_code == 401, "the token went with the password"
    assert any("was removed" in body for _, _, body in sent)


async def test_an_address_change_is_staged_confirmed_and_reaches_both_mailboxes(
    client, seeded, sent
):
    member = await _token(client, "member@example.org", "member-pass-phrase")

    r = await client.post(
        "/api/auth/email-change",
        json={"new_email": "maria.new@example.org"},
        headers=member,
    )
    assert r.status_code == 202, "202: nothing has moved yet"
    assert r.json()["pending_email"] == "maria.new@example.org"
    assert r.json()["email_change_expires_at"]

    to_new = [b for to, _, b in sent if to == "maria.new@example.org"]
    to_old = [b for to, _, b in sent if to == "member@example.org"]
    assert to_new, "the address being claimed gets the link"
    assert to_old, "the address being replaced gets a warning it can act on"
    token = to_new[0].split("/confirm-email/", 1)[1].split()[0].rstrip(".,)")

    async with db_session() as session:
        account = await users.get_by_email(session, "member@example.org")
        assert account.email_change_token == hashlib.sha256(token.encode()).hexdigest()
        assert account.email == "member@example.org", "not yet"

    # confirming needs no token of its own — the link IS the proof
    r = await client.post("/api/auth/email-change/confirm", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "maria.new@example.org"

    async with db_session() as session:
        moved = await users.get_by_email(session, "maria.new@example.org")
        assert moved is not None
        volunteer_id = moved.volunteer_id
        from volunteerdb.services import volunteers

        volunteer = await volunteers.get(session, volunteer_id)
        assert volunteer.email == "maria.new@example.org", (
            "one person, one address: the volunteer record moves too"
        )


async def test_a_pending_change_can_be_called_off(client, seeded, sent):
    member = await _token(client, "member@example.org", "member-pass-phrase")
    await client.post(
        "/api/auth/email-change",
        json={"new_email": "nope@example.org"},
        headers=member,
    )
    token = [b for to, _, b in sent if to == "nope@example.org"][0]
    token = token.split("/confirm-email/", 1)[1].split()[0].rstrip(".,)")

    assert (
        await client.delete("/api/auth/email-change", headers=member)
    ).status_code == 204

    r = await client.post("/api/auth/email-change/confirm", json={"token": token})
    assert r.status_code == 404, "cancelling killed the link"

    r = await client.get("/api/auth/me", headers=member)
    assert r.json()["pending_email"] is None


async def test_a_dead_confirmation_link_says_nothing_about_why(client, seeded):
    r = await client.post(
        "/api/auth/email-change/confirm", json={"token": "never-existed"}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "that link is not valid any more"


async def test_an_invite_can_be_spent_over_the_api(client, seeded, token_admin, sent):
    """The API minted invite tokens but nothing could spend one, so an account
    created over JSON had to be finished in a browser."""
    r = await client.post(
        "/api/users", json={"email": "fresh@example.org"}, headers=token_admin
    )
    assert r.status_code == 201, r.text
    token = r.json()["invite_token"]
    assert token, "an admin is handed the link"

    # the confidentiality agreement is refused in the service, not just collected
    r = await client.post(
        "/api/auth/redeem-invite",
        json={"token": token, "password": "cedar lamp figs"},
    )
    assert r.status_code == 422
    assert "confidentiality" in r.json()["detail"]

    r = await client.post(
        "/api/auth/redeem-invite",
        json={
            "token": token,
            "password": "cedar lamp figs",
            "agreed_to_confidentiality": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_password"] is True
    assert r.json()["invite_token"] is None, "spent, and never echoed anyway"

    assert await _token(client, "fresh@example.org", "cedar lamp figs")
    assert any("account is ready" in subject for _, subject, _ in sent), (
        "the welcome message goes out, as it does from the /invite page"
    )

    r = await client.post(
        "/api/auth/redeem-invite",
        json={"token": token, "agreed_to_confidentiality": True},
    )
    assert r.status_code == 404, "single use"


async def test_there_is_no_emailed_code_path_to_an_api_token(client, seeded):
    """Deliberately absent. An API token is issued against a password and
    revoked with it, so a mailbox round-trip must not produce one — an OTP-only
    account sets a password first if it wants API access."""
    for path in ("/api/auth/otp", "/api/auth/otp/request", "/api/auth/otp/verify"):
        r = await client.post(path, json={"email": "member@example.org"})
        assert r.status_code == 404, f"{path} should not exist"
