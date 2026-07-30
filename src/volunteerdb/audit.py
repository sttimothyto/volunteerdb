"""App-level CRUD audit trail via SQLAlchemy session events.

Complements the Postgres ``versioning()`` triggers (see
docs/explanation/history.md): the triggers remain the tamper-resistant record
of UPDATE/DELETE on versioned tables, while these listeners put a
human-readable line in the log for every read, insert, update, and delete —
including non-versioned tables — stamped with the acting user bound in log.py.

Registered on the Session *class*, so every session in the process is covered
(db.db_session, the API dependency's own session, history.fetch snapshots,
tests, scripts). Imported for side effects from db.py.
"""

import secrets
import warnings
from typing import Any

import structlog
from sqlalchemy import Join, event, inspect
from sqlalchemy.exc import SADeprecationWarning
from sqlalchemy.orm import ORMExecuteState, Session, UOWTransaction

from .config import settings
from .log import audit_log

_read_logger = structlog.get_logger("volunteerdb.audit")

# Column names whose VALUES must never reach a log line (AppUser credentials).
# Keep in sync with models.AppUser.
REDACTED_COLUMNS = {"password_hash", "otp_hash", "api_token", "invite_token"}
_MAX_VALUE_LEN = 120  # verbosity vs. one-line readability (notes/custom get truncated)


def _fmt(column: str, value: object) -> str:
    if column in REDACTED_COLUMNS and value is not None:
        return "«redacted»"
    text = repr(value)
    return text if len(text) <= _MAX_VALUE_LEN else text[:_MAX_VALUE_LEN] + "…"


def _txn(session: Session) -> str:
    """Short tag correlating a transaction's writes with its COMMIT/ROLLBACK line."""
    return session.info.setdefault("audit_txn", secrets.token_hex(3))


def _bump(session: Session) -> None:
    session.info["audit_writes"] = session.info.get("audit_writes", 0) + 1


def _ident(obj: object) -> tuple[str, str]:
    """("volunteer", "id=12") — table name and primary key of an instance."""
    mapper = inspect(obj).mapper
    pks = " ".join(f"{c.key}={getattr(obj, c.key, '?')}" for c in mapper.primary_key)
    return mapper.local_table.name, pks


def _row_values(obj: object) -> dict[str, str]:
    mapper = inspect(obj).mapper
    return {c.key: _fmt(c.key, getattr(obj, c.key, None)) for c in mapper.columns}


@event.listens_for(Session, "after_flush")
def _log_writes(session: Session, flush_context: UOWTransaction) -> None:
    # after_flush: new/dirty/deleted and attribute history are still pre-flush,
    # but INSERT statements have executed, so primary keys are assigned.
    for obj in session.new:
        table, pk = _ident(obj)
        audit_log("db.insert", table=table, pk=pk, txn=_txn(session), values=_row_values(obj))
        _bump(session)
    for obj in session.dirty:
        if not session.is_modified(obj, include_collections=False):
            continue
        state = inspect(obj)
        changes = {}
        for attr in state.mapper.column_attrs:
            history = state.attrs[attr.key].history
            if not history.has_changes():
                continue
            old = history.deleted[0] if history.deleted else None
            new = history.added[0] if history.added else None
            changes[attr.key] = f"{_fmt(attr.key, old)} → {_fmt(attr.key, new)}"
        if changes:
            table, pk = _ident(obj)
            audit_log("db.update", table=table, pk=pk, txn=_txn(session), changes=changes)
            _bump(session)
    for obj in session.deleted:
        table, pk = _ident(obj)
        audit_log("db.delete", table=table, pk=pk, txn=_txn(session), was=_row_values(obj))
        _bump(session)


@event.listens_for(Session, "after_commit")
def _log_commit(session: Session) -> None:
    writes = session.info.pop("audit_writes", 0)
    tag = session.info.pop("audit_txn", None)
    if writes:
        audit_log("db.commit", txn=tag, writes=writes)


@event.listens_for(Session, "after_rollback")
def _log_rollback(session: Session) -> None:
    writes = session.info.pop("audit_writes", 0)
    tag = session.info.pop("audit_txn", None)
    if writes:
        audit_log("db.rollback", txn=tag, writes_not_applied=writes)


def _from_names(from_obj: Any) -> list[str]:
    """Best-effort table names from a FROM clause, including joins/aliases."""
    if isinstance(from_obj, Join):
        return _from_names(from_obj.left) + _from_names(from_obj.right)
    name = getattr(from_obj, "name", None) or getattr(from_obj, "description", None)
    return [str(name) if name else "?"]


@event.listens_for(Session, "do_orm_execute")
def _log_execute(execute_state: ORMExecuteState) -> None:
    # Flush-generated DML never passes this event, so there is no double
    # logging with the after_flush listener above.
    stmt = execute_state.statement
    if execute_state.is_select:
        try:
            with warnings.catch_warnings():
                # get_final_froms() compile-inspects with the default dialect,
                # which warns about Postgres-only constructs like DISTINCT ON
                warnings.simplefilter("ignore", SADeprecationWarning)
                names = [n for f in stmt.get_final_froms() for n in _from_names(f)]
        except Exception:
            names = ["?"]
        if not names:  # e.g. SELECT set_config(...) — the history-trigger GUC call
            return
        tables = ", ".join(dict.fromkeys(names))
        if settings().log_level.upper() == "DEBUG":
            try:
                params = _fmt("", stmt.compile().params)
            except Exception:
                params = "?"
            _read_logger.info("db.read", table=tables, params=params)
        else:
            _read_logger.info("db.read", table=tables)
    elif execute_state.is_insert or execute_state.is_update or execute_state.is_delete:
        # Core DML bypassing the unit of work (today: capacity.set_config upsert)
        op = (
            "db.insert"
            if execute_state.is_insert
            else "db.update"
            if execute_state.is_update
            else "db.delete"
        )
        try:
            params = {k: _fmt(k, v) for k, v in stmt.compile().params.items()}
        except Exception:
            params = "?"
        table = getattr(getattr(stmt, "table", None), "name", "?")
        audit_log(op, table=table, core=True, txn=_txn(execute_state.session), values=params)
        _bump(execute_state.session)
