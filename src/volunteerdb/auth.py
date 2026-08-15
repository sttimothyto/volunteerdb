"""Password and token primitives. The *policy* (what may be chosen) lives in
passwords.py; this is the *storage* half of NIST SP 800-63B §3.1.1.2:

- argon2id, salted per password with 16 random bytes — well past the "SHALL be
  at least 32 bits" floor — and stored as a PHC string that carries the
  algorithm, version and cost factors with it, which is what "A reference to
  the password hashing scheme used, including the cost factor, SHOULD be stored
  for each password to allow migration to new algorithms and work factors"
  asks for. Raise the numbers below and every password rehashes on its owner's
  next sign-in (see needs_rehash).
- The cost factors are RFC 9106's second recommended option (64 MiB, t=3,
  p=4) and are argon2-cffi's own defaults, pinned here so that a library
  default drifting *down* cannot quietly weaken stored hashes.

Deliberately not done: the "additional iteration of a keyed hashing or
encryption operation" that §3.1.1.2 recommends. Its secret key "SHALL be stored
separately from the hashed passwords", and on a single-VM deployment with no
HSM the key would sit in /etc/volunteerdb/env on the same host as the database
— see docs/explanation/auth.md.
"""

import secrets

import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .passwords import normalize

_hasher = PasswordHasher(
    time_cost=3, memory_cost=64 * 1024, parallelism=4, hash_len=32, salt_len=16
)
# verified against when the account doesn't exist, so both paths cost one argon2 pass
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(normalize(password))


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, normalize(password))
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current cost factors. Checked on
    each successful sign-in, which is the only moment the password is in hand."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def burn_password_check(password: str) -> None:
    """Timing equalizer for unknown/passwordless accounts (enumeration resistance)."""
    verify_password(_DUMMY_HASH, password)


# Async handlers call the wrappers below: one argon2 pass is 100-400 ms of
# GIL-held CPU, and the app runs a single uvicorn worker, so a sync call
# stalls every other request and websocket for that window. Should concurrent
# 64 MiB hashes ever need bounding, an anyio.CapacityLimiter passed as
# `limiter=` here is the knob.


async def async_hash_password(password: str) -> str:
    return await anyio.to_thread.run_sync(hash_password, password)


async def async_verify_password(password_hash: str, password: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password, password_hash, password)


async def async_burn_password_check(password: str) -> None:
    await anyio.to_thread.run_sync(burn_password_check, password)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"
