"""Reading a Result in a test.

`ok(r)` is the value or a failure naming the error; `err_of(r)` is the error
or a failure naming the value; `refused(r, Kind, match=...)` is the shape most
service tests want -- the refusal is of this kind and its phrasing says this.
Test code never calls `.unwrap()`: that shim exists for unconverted callers
and leaves with them.
"""

import re

from volunteerdb.errors import DomainError, message
from volunteerdb.fp import Err, Ok


def ok[T](r: Ok[T] | Err) -> T:
    assert isinstance(r, Ok), f"expected Ok, got {r!r}"
    return r.value


def err_of[E](r: Ok | Err[E]) -> E:
    assert isinstance(r, Err), f"expected Err, got {r!r}"
    return r.error


def refused(r: Ok | Err, kind: type, *, match: str | None = None) -> DomainError:
    err = err_of(r)
    assert isinstance(err, kind), f"expected {kind.__name__}, got {err!r}"
    if match is not None:
        text = message(err)
        assert re.search(match, text), f"{match!r} not in {text!r}"
    return err
