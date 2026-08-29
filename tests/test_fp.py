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


def test_map_obeys_identity_and_composition():
    r = Ok(3)
    assert r.map(lambda x: x) == r
    f, g = (lambda x: x + 1), (lambda x: x * 2)
    assert r.map(f).map(g) == r.map(lambda x: g(f(x)))
    assert Err("e").map(f) == Err("e")


def test_bind_is_associative_and_short_circuits():
    def half(x: int) -> fp.Result[int, str]:
        return Ok(x // 2) if x % 2 == 0 else Err("odd")

    def dec(x: int) -> fp.Result[int, str]:
        return Ok(x - 1) if x > 0 else Err("zero")

    for start in (Ok(8), Ok(6), Ok(1), Err("e")):
        assert start.bind(half).bind(dec) == start.bind(lambda x: half(x).bind(dec))
    assert Ok(3).bind(half) == Err("odd")
    assert Err("first").bind(half) == Err("first")


def test_map_err_touches_only_the_error():
    assert Err(1).map_err(str) == Err("1")
    assert Ok(1).map_err(str) == Ok(1)


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


def test_ensure_and_require_are_gates():
    assert fp.ensure(True, "e") is None
    assert fp.ensure(False, "e") == Err("e")
    assert require(True) is None
    denied = require(False, "manage accounts")
    assert denied == Err(Forbidden("manage accounts"))
    # the walrus idiom: a refusal is truthy, permission is None
    assert bool(denied) and not require(True)


def test_some_or():
    assert fp.some_or(3, "absent") == Ok(3)
    assert fp.some_or(None, "absent") == Err("absent")
    assert fp.some_or(0, "absent") == Ok(0)  # falsy is not absent


def test_collect_is_fail_fast_in_order():
    assert fp.collect([Ok(1), Ok(2)]) == Ok([1, 2])
    assert fp.collect([]) == Ok([])
    seen = []

    def gen():
        for r in (Ok(1), Err("first"), Err("second")):
            seen.append(r)
            yield r

    assert fp.collect(gen()) == Err("first")
    assert seen == [Ok(1), Err("first")]  # stopped at the first error


def test_attempt_catches_only_what_it_is_told():
    assert fp.attempt(lambda: 1, ValueError, to_err=str) == Ok(1)

    def boom():
        raise ValueError("bad")

    assert fp.attempt(boom, ValueError, to_err=str) == Err("bad")
    with pytest.raises(ValueError):
        fp.attempt(boom, KeyError, to_err=str)


async def test_attempt_async():
    async def ok():
        return 1

    async def nope():
        raise LookupError("team 7 not found")

    async def bug():
        raise TypeError("programming error")

    assert await fp.attempt_async(ok(), LookupError, to_err=str) == Ok(1)


def test_expect_is_for_a_result_that_cannot_refuse():
    assert fp.expect(Ok(1)) == 1
    with pytest.raises(AssertionError, match="unexpected refusal"):
        fp.expect(Err(Invalid("too long")))


def test_as_result_wraps_plain_values_only():
    assert fp.as_result(1) == Ok(1)
    assert fp.as_result(Ok(1)) == Ok(1)
    assert fp.as_result(Err("e")) == Err("e")
