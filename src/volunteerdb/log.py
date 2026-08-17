"""Structlog setup: sinks, audit-aware filtering, stdlib interception, actor context.

Every log line carries ``user`` / ``ip`` / ``via`` (gui, api, or "-") bound via
contextvars, so the audit listeners in ``audit.py`` never need identity passed
in. Audit events (writes, auth, commit/rollback) are marked ``audit=True`` and
rank between INFO and WARNING, so the default AUDIT verbosity keeps them while
dropping routine reads and request lines.
"""

import logging
import logging.handlers
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

from .config import settings

AUDIT = 25  # between INFO (20) and WARNING (30)
# Keys must match config.LOG_LEVELS, which is what validates VDB_LOG_LEVEL;
# tests/test_config_surface.py pins the two together.
_MODE_NUM = {"DEBUG": 10, "INFO": 20, "AUDIT": AUDIT, "WARNING": 30, "ERROR": 40}


def _default_identity(
    logger: Any, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    event_dict.setdefault("user", "-")
    event_dict.setdefault("ip", "-")
    event_dict.setdefault("via", "-")
    return event_dict


def _promote_audit_level(
    logger: Any, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    if event_dict.pop("audit", False):
        event_dict["level"] = "audit"
    return event_dict


# Runs on both structlog events and intercepted stdlib records (uvicorn etc.),
# so every line renders identically. Also reused by the tests' capture fixture.
shared_processors: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    _default_identity,
    _promote_audit_level,
]


class _ModeFilter(logging.Filter):
    """Drop records below the configured verbosity; audit events rank as AUDIT."""

    def __init__(self, threshold: int) -> None:
        super().__init__()
        self.threshold = threshold

    def filter(self, record: logging.LogRecord) -> bool:
        # structlog-originated records carry their event dict as msg
        if isinstance(record.msg, dict) and record.msg.get("level") == "audit":
            return AUDIT >= self.threshold
        return record.levelno >= self.threshold


def _formatter(colors: bool) -> structlog.stdlib.ProcessorFormatter:
    return structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=colors),
        ],
        foreign_pre_chain=shared_processors,
    )


_configured = False


def init_logging() -> None:
    """Idempotent per process; called from main.create_app()/run() and scripts."""
    global _configured
    if _configured:
        return
    _configured = True
    s = settings()
    # Settings validates the name and upper-cases it, so this cannot miss.
    threshold = _MODE_NUM[s.log_level]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    mode_filter = _ModeFilter(threshold)
    console = logging.StreamHandler(
        sys.stderr
    )  # stderr: stdout stays clean for [MAIL] prints
    console.setFormatter(_formatter(colors=sys.stderr.isatty()))
    handlers: list[logging.Handler] = [console]
    if s.log_file:
        rotating = logging.handlers.RotatingFileHandler(
            s.log_file, maxBytes=10_000_000, backupCount=6
        )
        rotating.setFormatter(_formatter(colors=False))
        handlers.append(rotating)
    for handler in handlers:
        handler.addFilter(mode_filter)
    # Root at DEBUG: the handlers' mode filter decides, so audit events emitted
    # at stdlib INFO still pass when the threshold is AUDIT (25).
    logging.basicConfig(handlers=handlers, level=logging.DEBUG, force=True)


_audit_logger = structlog.get_logger("volunteerdb.audit")


def audit_log(event: str, **kw: Any) -> None:
    """Emit an audit event: visible at the default AUDIT verbosity."""
    _audit_logger.info(event, audit=True, **kw)


# --- WHO context -------------------------------------------------------------

_user_bound: ContextVar[bool] = ContextVar("vdb_user_bound", default=False)


@contextmanager
def bind_actor(user: str, ip: str = "-", via: str = "-") -> Iterator[None]:
    """Full identity ("id:email") for GUI/API request scopes."""
    token = _user_bound.set(True)
    try:
        with structlog.contextvars.bound_contextvars(user=user, ip=ip, via=via):
            yield
    finally:
        _user_bound.reset(token)


@contextmanager
def bind_fallback_user(user_id: int | None) -> Iterator[None]:
    """db_session()-level fallback: bare user id, only if nothing richer is bound."""
    if user_id is None or _user_bound.get():
        yield
        return
    with structlog.contextvars.bound_contextvars(user=str(user_id)):
        yield
