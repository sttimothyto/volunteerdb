"""The closed DomainError vocabulary and its one phrasing."""

import pytest
from sqlalchemy.exc import IntegrityError

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
    from_exception,
    message,
)
from volunteerdb.fp import Err
from volunteerdb.permissions import Forbidden as LegacyForbidden

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


def test_from_exception_covers_the_legacy_vocabulary():
    assert from_exception(LegacyForbidden("not allowed: edit")) == Forbidden("edit")
    assert from_exception(LookupError("team 4 not found")) == NotFound("team 4")
    assert from_exception(ValueError("bad input")) == Invalid("bad input")
    assert from_exception(IntegrityError("stmt", {}, Exception())) == Conflict()
    assert from_exception(errors.DomainErrorRaised(Throttled(1))) == Throttled(1)
    assert from_exception(TypeError("bug")) is None


def test_from_exception_keeps_subclass_identity():
    from volunteerdb.services.gcal import GcalError

    assert from_exception(GcalError("HTTP 500")) == External(
        "google calendar", "HTTP 500"
    )


def test_the_carrier_reads_as_its_message():
    exc = errors.DomainErrorRaised(NotFound("volunteer", 9))
    assert str(exc) == "volunteer 9 not found"
    assert exc.error == NotFound("volunteer", 9)
