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
returns (``errors.require``, ``ensure``)::

    if denied := require(actor.is_admin, "manage accounts"):
        return denied

No do-notation, no bind chains across awaits: the early return keeps the
happy path at the left margin and reads as Python.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from .errors import DomainError


class UnwrapError(Exception):
    """``Err.unwrap()`` on an error that is not a DomainError."""


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False

    def map[U](self, f: Callable[[T], U]) -> Ok[U]:
        return Ok(f(self.value))

    def map_err(self, f: Callable[[Any], Any]) -> Ok[T]:
        return self

    def bind[U, E](self, f: Callable[[T], Result[U, E]]) -> Result[U, E]:
        return f(self.value)

    def unwrap_or(self, default: Any) -> T:
        return self.value

    def unwrap(self) -> T:
        return self.value


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True

    def map(self, f: Callable[[Any], Any]) -> Err[E]:
        return self

    def map_err[F](self, f: Callable[[E], F]) -> Err[F]:
        return Err(f(self.error))

    def bind(self, f: Callable[[Any], Any]) -> Err[E]:
        return self

    def unwrap_or[T](self, default: T) -> T:
        return default

    def unwrap(self) -> NoReturn:
        """Transition shim: raise the error for a caller that still expects
        exceptions. A DomainError travels as ``errors.DomainErrorRaised``,
        which both front doors map exactly as they map the legacy exceptions.
        Deleted with the last raising caller."""
        from .errors import DomainErrorRaised, is_domain_error

        if is_domain_error(self.error):
            raise DomainErrorRaised(self.error)
        raise UnwrapError(self.error)


type Result[T, E] = Ok[T] | Err[E]


def ensure[E](condition: bool, error: E) -> Err[E] | None:
    """A gate: ``None`` when the condition holds, else the refusal.

    ``if denied := ensure(...): return denied`` -- the walrus keeps the check
    and its consequence on one line, and a result that is never bound is
    exactly the shape the authorization sweep refuses.
    """
    return None if condition else Err(error)


def some_or[T, E](value: T | None, error: E) -> Result[T, E]:
    """``T | None`` is the idiom for "absent"; this is where absence becomes
    an error, at the one call site that knows which error."""
    return Err(error) if value is None else Ok(value)


def collect[T, E](results: Iterable[Result[T, E]]) -> Result[list[T], E]:
    """All values, or the first error in iteration order (fail-fast)."""
    out: list[T] = []
    for r in results:
        if isinstance(r, Err):
            return r
        out.append(r.value)
    return Ok(out)


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


async def attempt_async[T, E](
    aw: Awaitable[T],
    *exc: type[BaseException],
    to_err: Callable[[BaseException], E],
) -> Result[T, E]:
    try:
        return Ok(await aw)
    except exc as e:
        return Err(to_err(e))


async def lift[T](aw: Awaitable[T]) -> Result[T, DomainError]:
    """Transition shim: call a not-yet-converted service from converted code.
    The legacy exception vocabulary becomes the matching DomainError; an
    exception outside that vocabulary is a bug and propagates."""
    from .errors import LEGACY_EXCEPTIONS, from_exception

    try:
        return Ok(await aw)
    except LEGACY_EXCEPTIONS as exc:
        err = from_exception(exc)
        if err is None:
            raise
        return Err(err)


def as_result[T, E](x: Result[T, E] | T) -> Result[T, E]:
    """Edges accept a Result or a plain value, so a route or handler can adopt
    the Result-aware helper before every service it calls has converted."""
    return x if isinstance(x, (Ok, Err)) else Ok(x)
