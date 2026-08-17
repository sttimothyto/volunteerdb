"""The password policy itself: what passwords.check accepts, and what storage
does with what it accepts. Pure unit tests — no database.

Each test names the NIST SP 800-63B §3.1.1.2 clause it pins down, because the
policy exists to satisfy that section and "why is 15 the number?" is the first
question anyone reading this will have.
"""

import unicodedata

import pytest
from argon2 import PasswordHasher

from volunteerdb import passwords
from volunteerdb.auth import hash_password, needs_rehash, verify_password
from volunteerdb.passwords import MAX_LENGTH, MIN_LENGTH, WeakPassword, check, problem


def test_minimum_length_is_fifteen():
    """ "SHALL require passwords that are used as a single-factor authentication
    mechanism to be a minimum of 15 characters in length"."""
    assert MIN_LENGTH == 15
    with pytest.raises(WeakPassword, match="too short"):
        check("14 chars: xyzw")
    check("otter lamp fig quilt")


def test_long_passwords_are_accepted_up_to_the_cap():
    """ "SHOULD permit a maximum password length of at least 64 characters" —
    the cap is only there to bound the work argon2 is asked to do."""
    assert MAX_LENGTH >= 64
    check("veldt quartz nymph broom sable fjord glint marmot plinth zephyr tundra")
    with pytest.raises(WeakPassword, match="too long"):
        check("veldt quartz nymph broom sable fjord glint marmot plinth zephyr " * 3)


def test_no_composition_rules():
    """ "SHALL NOT impose other composition rules (e.g., requiring mixtures of
    different character types)" — all-lowercase, all-digit and space-carrying
    passwords are all fine as long as they are not otherwise weak."""
    check("brindle ferry oxide")  # no capitals, digits or symbols
    check("8391746205518372")  # all digits, not a sequence
    check("   spaces  are  characters   ")


def test_unicode_is_accepted_and_counted_by_code_point():
    """ "SHOULD accept Unicode characters" and "Each Unicode code point SHALL be
    counted as a single character when evaluating password length"."""
    check("日本語のパスワードです、長いです")
    check("çöğüşéàñïåø-lumen")
    decomposed = unicodedata.normalize("NFD", "éclair-museé-voyage")
    assert len(decomposed) > len("éclair-museé-voyage")  # more code points
    check(decomposed)


def test_blocklist_matches_the_whole_password_not_substrings():
    """ "The entire password SHALL be subject to comparison, not substrings or
    words that might be contained therein"."""
    with pytest.raises(WeakPassword, match="well-known"):
        check("correct horse battery staple")
    check("horse battery cinnamon lathe")  # contains listed words, is not one


@pytest.mark.parametrize(
    "password",
    [
        "Passw0rd12345678",  # digits as decoration, 0 as o
        "P@ssword-2026-!!",  # symbols as letters
        "passwordpassword",  # the entry, doubled
        "MyPassword-2026!",
        "l3tm31nplease-77",
        "administrator123",
        "God-is-good-all-the-time",  # context: a parish's own vocabulary
    ],
)
def test_blocklist_sees_through_padding_and_leetspeak(password):
    with pytest.raises(WeakPassword, match="well-known"):
        check(password)


def test_context_specific_words_are_rejected():
    """ "Context-specific words, such as the name of the service, the username,
    and derivatives thereof"."""
    with pytest.raises(WeakPassword, match="email address or the name"):
        check("maria.alvarez2026", email="maria.alvarez@example.org")
    with pytest.raises(WeakPassword, match="email address or the name"):
        check("volunteerdb-2026")
    check("maria bakes rhubarb pies", email="maria.alvarez@example.org")


def test_the_organisations_own_names_are_context_words(monkeypatch):
    """Half of "the name of the service" depends on who is running the
    instance, so it is derived from the configured identity rather than
    listed. For "St. Timothy's" at sttimothyto.org this must reproduce
    exactly the four terms that used to be hardcoded — plus the mail domain,
    which was not.
    """
    from volunteerdb.config import settings
    from volunteerdb.passwords import _org_terms

    settings.cache_clear()
    terms = _org_terms(
        "St. Timothy's", "no-reply@sttimothyto.org", "https://vdb.sttimothyto.org"
    )
    assert {"sttimothy", "sttimothys", "sainttimothy", "sainttimothys"} <= terms
    assert "sttimothyto" in terms, "the mail domain is a context word too"

    # A parish with an unrelated name gets its own, and not St. Timothy's.
    other = _org_terms("Holy Family", "no-reply@holyfamily.example", "")
    assert "holyfamily" in other
    assert "sttimothy" not in other


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("1234567890123456", "straight along the keyboard"),
        ("qwertyuiopasdfgh", "straight along the keyboard"),
        ("abcabcabcabcabcabc", "one short pattern repeated"),
        ("aaaaaaaaaaaaaaaaaa", "one short pattern repeated"),
        ("ababababababababab", "one short pattern repeated"),
    ],
)
def test_structurally_weak_passwords_are_rejected(password, reason):
    with pytest.raises(WeakPassword, match=reason):
        check(password)


def test_every_rejection_states_a_reason_and_offers_guidance():
    """ "SHALL provide the reason for rejection" and "SHALL offer guidance to the
    subscriber to help the subscriber choose a strong password"."""
    for weak in ("short", "passwordpassword", "1234567890123456"):
        message = problem(weak)
        assert message and passwords.GUIDANCE in message
    assert problem("brindle ferry oxide") is None


def test_hashing_normalizes_so_the_same_phrase_verifies_either_way():
    """ "the verifier SHOULD apply the normalization process for stabilized
    strings using [NFC]... before hashing the byte string"."""
    composed = unicodedata.normalize("NFC", "éclair-museé-voyage")
    decomposed = unicodedata.normalize("NFD", "éclair-museé-voyage")
    assert composed != decomposed  # different bytes, same password
    stored = hash_password(decomposed)
    assert verify_password(stored, composed)
    assert verify_password(stored, decomposed)
    assert not verify_password(stored, "eclair-musee-voyage")


def test_stored_hash_records_its_own_cost_factors():
    """ "A reference to the password hashing scheme used, including the cost
    factor, SHOULD be stored for each password to allow migration to new
    algorithms and work factors" — the argon2 PHC string is that reference."""
    stored = hash_password("brindle ferry oxide")
    assert stored.startswith("$argon2id$")
    assert "m=65536,t=3,p=4" in stored
    assert not needs_rehash(stored)


def test_weaker_stored_hashes_are_flagged_for_rehash():
    """ "The chosen cost factor... SHOULD be increased over time" — raising the
    numbers has to actually re-stretch the passwords already on disk."""
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("x" * 20)
    assert needs_rehash(weak)
    assert not needs_rehash("not-an-argon2-hash-at-all")  # nothing to upgrade
