"""The closed set of ways a request can be refused, as values.

Each variant is a frozen dataclass, not an Exception: it is data a service
returns inside ``fp.Err`` and an edge translates -- once, in one place per
front door (``api/deps.py``, ``ui/context.py``, ``jobs``). ``message()`` is the
single user-facing phrasing, which is how the JSON API's ``detail`` and the
GUI's toast say the same thing about the same refusal.

``DomainErrorRaised`` and ``from_exception`` are the transition shims: while
services convert one module at a time, a converted callee's ``Err.unwrap()``
raises the carrier for an unconverted caller, and ``fp.lift`` turns an
unconverted callee's exception into the matching value. Both are deleted with
the last raising service.
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

DOMAIN_ERRORS: tuple[type, ...] = (
    Forbidden,
    NotFound,
    Invalid,
    Conflict,
    Throttled,
    External,
    WeakPassword,
    QueryError,
    BadCredentials,
)


def is_domain_error(x: object) -> bool:
    return isinstance(x, DOMAIN_ERRORS)


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


# --- transition shims (deleted with the last raising service) ---------------


class DomainErrorRaised(Exception):
    """A DomainError travelling as an exception, for a caller that has not
    converted yet. Carries the value; ``str()`` is its message."""

    def __init__(self, error: DomainError) -> None:
        super().__init__(message(error))
        self.error = error


def _legacy_exceptions() -> tuple[type[BaseException], ...]:
    from sqlalchemy.exc import IntegrityError

    return (
        DomainErrorRaised,
        PermissionError,
        LookupError,
        ValueError,
        IntegrityError,
        RuntimeError,
    )


LEGACY_EXCEPTIONS: tuple[type[BaseException], ...] = _legacy_exceptions()


def from_exception(exc: BaseException) -> DomainError | None:
    """The DomainError a legacy exception meant, or None for one outside the
    vocabulary (a bug, which should keep propagating)."""
    from sqlalchemy.exc import IntegrityError

    if isinstance(exc, DomainErrorRaised):
        return exc.error
    text = str(exc)
    if isinstance(exc, PermissionError):
        return Forbidden(text.removeprefix("not allowed: "))
    if isinstance(exc, LookupError):
        return NotFound(text.removesuffix(" not found"))
    if isinstance(exc, IntegrityError):
        return Conflict()
    if isinstance(exc, ValueError):
        # WeakPassword and QueryError subclass ValueError so the old edge
        # handlers caught them; keep their identity as values.
        name = type(exc).__name__
        if name == "WeakPassword":
            return WeakPassword(text)
        if name == "QueryError":
            return QueryError(text)
        return Invalid(text)
    if isinstance(exc, RuntimeError):
        name = type(exc).__name__
        service = {
            "GSheetsError": "google sheets",
            "GcalError": "google calendar",
            "GoogleApiError": "google",
        }.get(name)
        if service is not None:
            return External(service, text)
    return None
