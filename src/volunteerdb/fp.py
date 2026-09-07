"""Result: what a fallible function returns instead of raising.

A function that can fail returns ``Ok(value)`` or ``Err(error)``; the caller
decides what failure means at the point where it has the context to decide.
Nothing here knows about the domain -- ``E`` is whatever the caller chose,
though across this codebase it is always ``errors.DomainError``.

The propagation idiom is deliberately plain, and there is exactly one of it::

    r = await team_service.get(session, team_id)
    if isinstance(r, Err):
        return r
    team = r.value

``match`` is for branching on the *error*::

    match r:
        case Err(NotFound()): ...
        case Err(Forbidden(what)): ...

Never truth-test a Result: ``Ok(None)`` and ``Err(...)`` are both truthy, on
purpose -- an ``if r:`` that meant ``is_ok`` would silently pass every error.
The one thing that IS meant to be truth-tested is the ``Err | None`` a gate
returns (``errors.require``)::

    if denied := require(actor.is_admin, "manage accounts"):
        return denied

No do-notation, no bind chains across awaits: the early return keeps the
happy path at the left margin and reads as Python.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Generic, TypeVar

# Ok and Err are covariant in what they carry, and have to say so the old way.
# The PEP 695 spelling (``class Err[E]``) infers variance from how the type
# parameter is used, and an annotated attribute counts as settable however
# frozen the dataclass is -- so ``Err[E]`` comes out invariant, and every
# ``return invalid(...)`` from a function returning ``Result[Event, DomainError]``
# is a type error, because ``Err[Invalid]`` is not an ``Err[DomainError]``.
# That was 231 of the 368 diagnostics ty had on this tree. An explicit
# ``covariant=True`` TypeVar is the only way to state it until PEP 696-era
# syntax grows one; the classes below are otherwise unchanged.
T_co = TypeVar("T_co", covariant=True)
E_co = TypeVar("E_co", covariant=True)


class _Unset:
    """The type of UNSET: one value, its own repr, never equal to anything."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


# "This argument was not given", for an update whose every field is optional:
# ``None`` is a value there (clear the field) and UNSET is its absence. A
# service spells ``notes: str | None | object = UNSET`` and tests
# ``notes is not UNSET``. One sentinel for the tree, so the idiom reads the
# same in every service.
UNSET: Final = _Unset()


@dataclass(frozen=True, slots=True)
class Ok(Generic[T_co]):
    value: T_co

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False

    def unwrap_or(self, default: Any) -> T_co:
        return self.value


@dataclass(frozen=True, slots=True)
class Err(Generic[E_co]):
    error: E_co

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True

    def unwrap_or[T](self, default: T) -> T:
        return default


type Result[T, E] = Ok[T] | Err[E]


def attempt[T, E](
    fn: Callable[[], T],
    *exc: type[BaseException],
    to_err: Callable[[BaseException], E],
) -> Result[T, E]:
    """Run raising code as a Result. Only the named exception types are
    caught -- everything else is a programming error and keeps propagating."""
    try:
        return Ok(fn())
    except exc as e:
        return Err(to_err(e))


def expect[T](result: Result[T, Any]) -> T:
    """The value of a Result that cannot be an Err here -- a read guarded by
    the very right the service checks, an internal caller acting for nobody.
    An Err is then a bug, not a refusal, and says so."""
    assert isinstance(result, Ok), f"unexpected refusal: {result!r}"
    return result.value
