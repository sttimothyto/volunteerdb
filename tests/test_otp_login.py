"""Service-level tests: email OTP login, optional-password invites, mail, sessions."""

from datetime import UTC, datetime, timedelta

from volunteerdb.config import Settings
from volunteerdb.db import db_session
from volunteerdb.services import mail, users
from volunteerdb.ui.context import session_expired


async def test_start_otp_login_unknown_or_inactive(database):
    async with db_session() as session:
        assert await users.start_otp_login(session, "nobody@example.org") is None
        u, _ = await users.create(session, "off@example.org")
        await users.set_flags(session, u.id, is_active=False)
        assert await users.start_otp_login(session, "off@example.org") is None


async def test_otp_round_trip_throttle_and_invite_clear(database):
    async with db_session() as session:
        await users.create(session, "otp@example.org")  # no password -> invite token
        user, code = await users.start_otp_login(session, "otp@example.org")
        assert code is not None and len(code) == 6 and code.isdigit()
        assert user.otp_hash is not None and user.otp_hash != code
        assert user.otp_attempts == 0
        assert (
            timedelta(minutes=9)
            < user.otp_expires_at - datetime.now(UTC)
            <= timedelta(minutes=10)
        )

        old_hash = user.otp_hash
        again_user, again_code = await users.start_otp_login(session, "otp@example.org")
        assert again_user.id == user.id and again_code is None  # throttled
        assert user.otp_hash == old_hash

        wrong = "000000" if code != "000000" else "111111"
        assert user.invite_token is not None
        assert await users.verify_otp(session, "otp@example.org", wrong) is None
        assert user.otp_attempts == 1

        verified = await users.verify_otp(session, "otp@example.org", code)
        assert verified is not None and verified.id == user.id
        assert user.otp_hash is None and user.otp_sent_at is None
        assert user.otp_expires_at is None and user.otp_attempts == 0
        assert user.invite_token is None  # email possession redeemed the invite
        await session.refresh(user)
        assert user.last_login_at is not None


async def test_otp_lockout_then_fresh_code(database):
    async with db_session() as session:
        await users.create(session, "lock@example.org")
        user, code = await users.start_otp_login(session, "lock@example.org")
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(users.OTP_MAX_ATTEMPTS):
            assert await users.verify_otp(session, "lock@example.org", wrong) is None
        assert user.otp_attempts == users.OTP_MAX_ATTEMPTS
        # locked out: even the correct code is rejected now
        assert await users.verify_otp(session, "lock@example.org", code) is None

        user.otp_sent_at = None  # skip the resend throttle
        await session.flush()
        user2, code2 = await users.start_otp_login(session, "lock@example.org")
        assert code2 is not None and user2.otp_attempts == 0
        assert await users.verify_otp(session, "lock@example.org", code2) is not None


async def test_otp_expired_code_rejected(database):
    async with db_session() as session:
        await users.create(session, "exp@example.org")
        user, code = await users.start_otp_login(session, "exp@example.org")
        user.otp_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()
        assert await users.verify_otp(session, "exp@example.org", code) is None


async def test_redeem_invite_password_optional(database):
    async with db_session() as session:
        a, a_token = await users.create(session, "nopw@example.org")
        b, b_token = await users.create(session, "withpw@example.org")

        ra = await users.redeem_invite(
            session, a_token, None, agreed_to_confidentiality=True
        )
        assert ra is not None
        assert ra.password_hash is None and ra.invite_token is None  # OTP-only account
        assert await users.authenticate(session, "nopw@example.org", "anything") is None

        rb = await users.redeem_invite(
            session,
            b_token,
            "long-enough-phrase",
            agreed_to_confidentiality=True,
        )
        assert rb is not None and rb.password_hash is not None
        assert (
            await users.authenticate(
                session, "withpw@example.org", "long-enough-phrase"
            )
            is not None
        )


async def test_mail_dev_mode_and_builders(monkeypatch, capsys):
    """With no API key nothing is sent — but the BODY only reaches the log
    under VDB_DEBUG_MAIL (or VDB_RELOAD, which means `make dev`). These bodies
    carry sign-in codes and invite links, so an instance that merely forgot the
    API key must not write every credential it issues into journald."""
    monkeypatch.setattr(mail, "settings", lambda: Settings(smtp2go_api_key=""))
    assert await mail.send_email("x@example.org", "Subj", "Body")  # no network
    out = capsys.readouterr().out
    assert "Body" not in out, "no credentials on stdout without opting in"

    monkeypatch.setattr(
        mail, "settings", lambda: Settings(smtp2go_api_key="", debug_mail=True)
    )
    assert await mail.send_email("x@example.org", "Subj", "Body")
    out = capsys.readouterr().out
    assert "[MAIL]" in out and "x@example.org" in out and "Body" in out

    monkeypatch.setattr(
        mail, "settings", lambda: Settings(smtp2go_api_key="", reload=True)
    )
    assert await mail.send_email("x@example.org", "Subj", "Body")
    assert "Body" in capsys.readouterr().out, "`make dev` needs nothing set"

    subject, body = mail.otp_email("123456")
    assert "123456" in subject and "10 minutes" in body
    subject, body = mail.invite_email("https://x/invite/tok")
    assert "https://x/invite/tok" in body and "optional" in body
    assert "7 days" in body, "the link's lifetime is stated where it is handed out"
    subject, body = mail.password_changed_email("https://x/login")
    assert "was just changed" in body and "parish office" in body
    subject, body = mail.password_changed_email("https://x/login", removed=True)
    assert "was removed" in body
    subject, body = mail.welcome_email("https://x/login", has_password=False)
    assert "one-time code" in body
    subject, body = mail.welcome_email("https://x/login", has_password=True)
    assert "password" in body


def test_session_expired():
    assert session_expired(None)
    assert session_expired("")
    assert session_expired("not-a-timestamp")
    assert session_expired((datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    assert not session_expired((datetime.now(UTC) + timedelta(days=1)).isoformat())
