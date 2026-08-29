"""The closed DomainError vocabulary and its one phrasing."""

import pytest

from volunteerdb import errors
from volunteerdb.errors import (
    BadCredentials,
    Conflict,
    External,
    Forbidden,
    Invalid,
    NotFound,
    QueryError,
    Throttled,
    WeakPassword,
    message,
)
from volunteerdb.fp import Err

pytestmark = pytest.mark.pure


@pytest.mark.parametrize(
    "err, text",
    [
        (Forbidden("manage accounts"), "not allowed: manage accounts"),
        (NotFound("team", 3), "team 3 not found"),
        (NotFound("assignment"), "assignment not found"),
        (Invalid("starts after it ends"), "starts after it ends"),
        (Conflict(), "conflicts with existing data"),
        (Throttled(900), "too many attempts — try again later"),
        (Throttled(900, "sign in"), "too many attempts to sign in — try again later"),
        (External("google sheets", "HTTP 429"), "google sheets: HTTP 429"),
        (WeakPassword("too short"), "too short"),
        (QueryError("unknown field"), "unknown field"),
        (BadCredentials("no such account"), "invalid email or password"),
    ],
)
def test_every_variant_has_one_phrasing(err, text):
    assert message(err) == text
    assert errors.is_domain_error(err)


def test_constructors_return_err_values():
    assert errors.not_found("team", 1) == Err(NotFound("team", 1))
    assert errors.invalid("nope", "name") == Err(Invalid("nope", "name"))
    assert errors.require(False, "x") == Err(Forbidden("x"))


def test_variants_are_values():
    assert NotFound("team", 1) == NotFound("team", 1)
    assert hash(Invalid("a")) == hash(Invalid("a"))
    with pytest.raises(AttributeError):
        Invalid("a").message = "b"  # type: ignore[misc]
