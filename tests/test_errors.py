"""The closed DomainError vocabulary and its one phrasing."""

import pytest

from volunteerdb import errors
from volunteerdb.api.deps import status_of, to_http
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


def test_constructors_return_err_values():
    assert errors.not_found("team", 1) == Err(NotFound("team", 1))
    assert errors.invalid("nope", "name") == Err(Invalid("nope", "name"))
    assert errors.require(False, "x") == Err(Forbidden("x"))


def test_variants_are_values():
    with pytest.raises(AttributeError):
        Invalid("a").message = "b"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("err", "status"),
    [
        (Forbidden("edit"), 403),
        (NotFound("team", 1), 404),
        (Invalid("bad"), 422),
        (WeakPassword("short"), 422),
        (QueryError("unknown field"), 422),
        (Conflict(), 409),
        (Throttled(30, "sign in"), 429),
        (External("google docs", "500"), 502),
        (BadCredentials("wrong password"), 401),
    ],
)
def test_every_refusal_has_one_status(err, status):
    """The closed sum, mapped once. A new variant fails here before it can
    surface as a 500 somewhere in the API."""
    assert status_of(err) == status
    http = to_http(err)
    assert http.status_code == status and http.detail == message(err)


def test_the_headers_that_ride_with_a_refusal():
    assert to_http(BadCredentials("x")).headers == {"WWW-Authenticate": "Bearer"}
    assert to_http(Throttled(30)).headers == {"Retry-After": "30"}
    assert to_http(NotFound("team")).headers is None
