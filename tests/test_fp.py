"""fp.Result: the laws the toolkit promises, with no database in sight."""

import pytest

from volunteerdb import fp
from volunteerdb.errors import (
    Forbidden,
    Invalid,
    NotFound,
    require,
)
from volunteerdb.fp import Err, Ok

pytestmark = pytest.mark.pure


def test_unwrap_or_and_predicates():
    assert Ok(1).unwrap_or(9) == 1
    assert Err("x").unwrap_or(9) == 9
    assert Ok(1).is_ok() and not Ok(1).is_err()
    assert Err(1).is_err() and not Err(1).is_ok()


def test_results_are_never_falsy():
    """Ok(None) and Err(...) are both truthy on purpose: an `if r:` that meant
    `is_ok` would silently pass every error."""
    assert Ok(None)
    assert Err(None)


def test_results_match_positionally():
    match Ok(5):
        case Ok(v):
            assert v == 5
        case _:  # pragma: no cover
            raise AssertionError
    match Err(NotFound("team", 1)):
        case Err(NotFound(kind, key)):
            assert (kind, key) == ("team", 1)
        case _:  # pragma: no cover
            raise AssertionError


def test_require_is_a_gate():
    assert require(True) is None
    denied = require(False, "manage accounts")
    assert denied == Err(Forbidden("manage accounts"))
    # the walrus idiom: a refusal is truthy, permission is None
    assert bool(denied) and not require(True)


def test_attempt_catches_only_what_it_is_told():
    assert fp.attempt(lambda: 1, ValueError, to_err=str) == Ok(1)

    def boom():
        raise ValueError("bad")

    assert fp.attempt(boom, ValueError, to_err=str) == Err("bad")
    with pytest.raises(ValueError):
        fp.attempt(boom, KeyError, to_err=str)


def test_expect_is_for_a_result_that_cannot_refuse():
    assert fp.expect(Ok(1)) == 1
    with pytest.raises(AssertionError, match="unexpected refusal"):
        fp.expect(Err(Invalid("too long")))


def test_as_result_wraps_plain_values_only():
    assert fp.as_result(1) == Ok(1)
    assert fp.as_result(Ok(1)) == Ok(1)
    assert fp.as_result(Err("e")) == Err("e")
