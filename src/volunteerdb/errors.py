"""The closed set of ways a request can be refused, as values.

Each variant is a frozen dataclass, not an Exception: it is data a service
returns inside ``fp.Err`` and an edge translates -- once, in one place per
front door (``api/deps.py``, ``ui/context.py``, ``jobs``). ``message()`` is the
single user-facing phrasing, which is how the JSON API's ``detail`` and the
GUI's toast say the same thing about the same refusal.

The transition shims that carried the conversion one module at a time
(``DomainErrorRaised``, ``from_exception``, ``fp.lift``, ``Err.unwrap()``) are
gone: every service returns a ``Result``, and ``fp.expect()`` is what a guarded
read uses where an ``Err`` would be a bug rather than a refusal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fp import Err


@dataclass(frozen=True, slots=True)
class Forbidden:
    what: str


@dataclass(frozen=True, slots=True)
class NotFound:
    kind: str
    key: object = None


@dataclass(frozen=True, slots=True)
class Invalid:
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class Conflict:
    message: str = "conflicts with existing data"


@dataclass(frozen=True, slots=True)
class Throttled:
    retry_after_s: int
    what: str = ""


@dataclass(frozen=True, slots=True)
class External:
    service: str
    message: str


@dataclass(frozen=True, slots=True)
class WeakPassword:
    message: str


@dataclass(frozen=True, slots=True)
class QueryError:
    message: str


@dataclass(frozen=True, slots=True)
class BadCredentials:
    reason: str  # for the log line; the client sees one phrase, whatever the reason


type DomainError = (
    Forbidden
    | NotFound
    | Invalid
    | Conflict
    | Throttled
    | External
    | WeakPassword
    | QueryError
    | BadCredentials
)


def message(err: DomainError) -> str:
    """The one user-facing phrasing of a refusal."""
    match err:
        case Forbidden(what):
            return f"not allowed: {what}"
        case NotFound(kind, None):
            return f"{kind} not found"
        case NotFound(kind, key):
            return f"{kind} {key} not found"
        case Invalid(text, _) | Conflict(text) | WeakPassword(text) | QueryError(text):
            return text
        case Throttled(_, what):
            return (
                f"too many attempts to {what} — try again later"
                if what
                else "too many attempts — try again later"
            )
        case External(service, text):
            return f"{service}: {text}"
        case BadCredentials():
            return "invalid email or password"
    raise AssertionError(f"not a DomainError: {err!r}")  # pragma: no cover


# --- constructors: the short spellings services use -------------------------


def require(condition: bool, what: str = "this action") -> Err[Forbidden] | None:
    """The authorization gate, as a value: ``None`` when allowed.

    ``if denied := require(actor.is_admin, "manage accounts"): return denied``
    """
    return None if condition else Err(Forbidden(what))


def not_found(kind: str, key: object = None) -> Err[NotFound]:
    return Err(NotFound(kind, key))


def invalid(message: str, field: str | None = None) -> Err[Invalid]:
    return Err(Invalid(message, field))
