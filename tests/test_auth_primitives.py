"""Unit tests for the crypto/auth primitives (no database access)."""

import re

from volunteerdb.auth import (
    burn_password_check,
    hash_password,
    new_otp_code,
    new_token,
    verify_password,
)
from volunteerdb.services.users import _token_digest


def test_password_hash_roundtrip():
    hashed = hash_password("correct horse")
    assert hashed != "correct horse"
    assert verify_password(hashed, "correct horse") is True
    assert verify_password(hashed, "wrong horse") is False


def test_verify_password_garbage_hash_is_false():
    assert verify_password("not-an-argon2-hash", "anything") is False
    assert verify_password("", "anything") is False


def test_burn_password_check_never_raises():
    assert burn_password_check("whatever") is None


def test_new_otp_code_format():
    for _ in range(50):
        assert re.fullmatch(r"\d{6}", new_otp_code()), "always six digits, zero-padded"


def test_token_digest_deterministic():
    token = new_token()
    digest = _token_digest(token)
    assert re.fullmatch(r"[0-9a-f]{64}", digest), "SHA-256 hex"
    assert digest == _token_digest(token)
    assert digest != _token_digest(token + "x")
