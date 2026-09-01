"""Service-level tests: email OTP login, optional-password invites, mail, sessions."""

from datetime import UTC, datetime, timedelta

import pytest

from volunteerdb import errors
from volunteerdb.config import Settings
from volunteerdb.env import LoggingMailer
from volunteerdb.services import mail, users
from volunteerdb.ui.context import session_expired

from tests import mint
from tests.conftest import db_session
from tests.fp_helpers import done, ok, otp_started, refused
from tests.test_users_service import EDGE, EDGE_IDS


async def test_start_otp_login_unknown_or_inactive(database):
    async with db_session() as session:
        refused(
            await users.start_otp_login(
                session, "nobody@example.org", now=mint.now(), code=mint.code()
            ),
            errors.NotFound,
        )
        u, _ = ok(
            await users.create(session, "off@example.org", invite=mint.fresh_invite())
        )
        ok(await users.set_flags(session, u.id, is_active=False))
        refused(
            await users.start_otp_login(
                session, "off@example.org", now=mint.now(), code=mint.code()
            ),
            errors.NotFound,
        )


async def test_otp_round_trip_throttle_and_invite_clear(database):
    async with db_session() as session:
        ok(
            await users.create(session, "otp@example.org", invite=mint.fresh_invite())
        )  # no password -> invite token
        user, code = otp_started(
            await users.start_otp_login(
                session, "otp@example.org", now=mint.now(), code=mint.code()
            )
        )
        assert code is not None and len(code) == 6 and code.isdigit()
        assert user.otp_hash is not None and user.otp_hash != code
        assert user.otp_attempts == 0
        assert (
            timedelta(minutes=9)
            < user.otp_expires_at - datetime.now(UTC)
            <= timedelta(minutes=10)
        )

        old_hash = user.otp_hash
        again_user, again_code = otp_started(
            await users.start_otp_login(
                session, "otp@example.org", now=mint.now(), code=mint.code()
            )
        )
        assert again_user.id == user.id and again_code is None  # throttled
        assert user.otp_hash == old_hash

        wrong = "000000" if code != "000000" else "111111"
        assert user.invite_token is not None
        assert (
            refused(
                await users.verify_otp(
                    session, "otp@example.org", wrong, now=mint.now()
                ),
                errors.BadCredentials,
            ).reason
            == "wrong code"
        )
        assert user.otp_attempts == 1

        verified = ok(
            await users.verify_otp(session, "otp@example.org", code, now=mint.now())
        )
        assert verified is not None and verified.id == user.id
        assert user.otp_hash is None and user.otp_sent_at is None
        assert user.otp_expires_at is None and user.otp_attempts == 0
        assert user.invite_token is None  # email possession redeemed the invite
        await session.refresh(user)
        assert user.last_login_at is not None


async def test_otp_lockout_then_fresh_code(database):
    async with db_session() as session:
        ok(await users.create(session, "lock@example.org", invite=mint.fresh_invite()))
        user, code = otp_started(
            await users.start_otp_login(
                session, "lock@example.org", now=mint.now(), code=mint.code()
            )
        )
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(users.OTP_MAX_ATTEMPTS):
            err = refused(
                await users.verify_otp(
                    session, "lock@example.org", wrong, now=mint.now()
                ),
                errors.BadCredentials,
            )
            assert err.reason == "wrong code"
        assert user.otp_attempts == users.OTP_MAX_ATTEMPTS
        # locked out: even the correct code is rejected now, and the log line
        # says why while the client is told the one neutral phrase
        err = refused(
            await users.verify_otp(session, "lock@example.org", code, now=mint.now()),
            errors.BadCredentials,
        )
        assert err.reason == "too many attempts"
        assert errors.message(err) == "invalid email or password"

        user.otp_sent_at = None  # skip the resend throttle
        await session.flush()
        user2, code2 = otp_started(
            await users.start_otp_login(
                session, "lock@example.org", now=mint.now(), code=mint.code()
            )
        )
        assert code2 is not None and user2.otp_attempts == 0
        assert (
            await users.verify_otp(session, "lock@example.org", code2, now=mint.now())
        ).is_ok()


async def test_otp_expired_code_rejected(database, clock):
    async with db_session() as session:
        ok(await users.create(session, "exp@example.org", invite=mint.fresh_invite()))
        user, code = otp_started(
            await users.start_otp_login(
                session, "exp@example.org", now=clock.now(), code=mint.code()
            )
        )
        clock.advance(seconds=users.OTP_TTL.total_seconds() + 1)
        assert (
            await users.verify_otp(session, "exp@example.org", code, now=clock.now())
        ).is_err()


async def test_redeem_invite_password_optional(database):
    async with db_session() as session:
        a, a_token = ok(
            await users.create(session, "nopw@example.org", invite=mint.fresh_invite())
        )
        b, b_token = ok(
            await users.create(
                session, "withpw@example.org", invite=mint.fresh_invite()
            )
        )

        ra = done(
            await users.redeem_invite(
                session, a_token, None, agreed_to_confidentiality=True, now=mint.now()
            )
        ).value
        assert ra.password_hash is None and ra.invite_token is None  # OTP-only account
        assert (
            refused(
                await users.authenticate(
                    session, "nopw@example.org", "anything", now=mint.now()
                ),
                errors.BadCredentials,
            ).reason
            == "no password-bearing account at that address"
        )

        rb = done(
            await users.redeem_invite(
                session,
                b_token,
                "long-enough-phrase",
                agreed_to_confidentiality=True,
                now=mint.now(),
            )
        ).value
        assert rb.password_hash is not None
        assert (
            await users.authenticate(
                session, "withpw@example.org", "long-enough-phrase", now=mint.now()
            )
        ).is_ok()


async def test_mail_dev_mode_and_builders(capsys):
    """With no API key nothing is sent — but the BODY only reaches the log
    under VDB_DEBUG_MAIL (or VDB_RELOAD, which means `make dev`). These bodies
    carry sign-in codes and invite links, so an instance that merely forgot the
    API key must not write every credential it issues into journald."""
    quiet = LoggingMailer(Settings(smtp2go_api_key=""))
    assert await quiet.send("x@example.org", "Subj", "Body")  # no network
    out = capsys.readouterr().out
    assert "Body" not in out, "no credentials on stdout without opting in"

    loud = LoggingMailer(Settings(smtp2go_api_key="", debug_mail=True))
    assert await loud.send("x@example.org", "Subj", "Body")
    out = capsys.readouterr().out
    assert "[MAIL]" in out and "x@example.org" in out and "Body" in out

    dev = LoggingMailer(Settings(smtp2go_api_key="", reload=True))
    assert await dev.send("x@example.org", "Subj", "Body")
    assert "Body" in capsys.readouterr().out, "`make dev` needs nothing set"

    subject, body = mail.otp_email("123456")
    assert "123456" in subject and "10 minutes" in body
    subject, body = mail.invite_email("https://x/invite/tok", ctx=mint.mail_context())
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
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert session_expired(None, now)
    assert session_expired("", now)
    assert session_expired("not-a-timestamp", now)
    assert session_expired((now - timedelta(seconds=1)).isoformat(), now)
    assert not session_expired((now + timedelta(days=1)).isoformat(), now)


@pytest.mark.parametrize(
    ("delta", "expired"),
    [
        (timedelta(microseconds=-1), True),
        (timedelta(0), True),
        (timedelta(microseconds=1), False),
    ],
    ids=["just past", "at the instant", "just ahead"],
)
def test_a_session_is_dead_at_the_instant_it_names(delta, expired):
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert session_expired((now + delta).isoformat(), now) is expired


@pytest.mark.parametrize(("delta", "live"), EDGE, ids=EDGE_IDS)
async def test_a_code_is_dead_at_the_instant_it_names(database, clock, delta, live):
    async with db_session() as session:
        ok(await users.create(session, "edge@example.org", invite=mint.fresh_invite()))
        _user, code = otp_started(
            await users.start_otp_login(
                session, "edge@example.org", now=clock.now(), code=mint.code()
            )
        )
        at = clock.now() + users.OTP_TTL + delta
        result = await users.verify_otp(session, "edge@example.org", code, now=at)
        if live:
            ok(result)
        else:
            assert refused(result, errors.BadCredentials).reason == "code expired"


@pytest.mark.parametrize(
    ("delta", "resent"),
    [
        (timedelta(seconds=-1), False),
        (timedelta(0), True),
        (timedelta(seconds=1), True),
    ],
    ids=["59 s", "60 s", "61 s"],
)
async def test_a_second_code_is_issued_once_the_resend_interval_has_passed(
    database, clock, delta, resent
):
    """A live code asked for again within the minute is not re-sent -- each
    one is a message spent -- and at the minute it is."""
    async with db_session() as session:
        ok(await users.create(session, "again@example.org", invite=mint.fresh_invite()))
        otp_started(
            await users.start_otp_login(
                session, "again@example.org", now=clock.now(), code=mint.code()
            )
        )
        at = clock.now() + users.OTP_RESEND_INTERVAL + delta
        _user, code = otp_started(
            await users.start_otp_login(
                session, "again@example.org", now=at, code=mint.code()
            )
        )
        assert (code is not None) is resent
