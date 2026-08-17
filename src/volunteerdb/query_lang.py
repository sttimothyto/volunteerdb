"""SQL WHERE-clause filters for the search boxes.

The search boxes accept either plain text (substring search, as ever) or a
SQL boolean expression such as ``phone LIKE '555%' AND team = 'Liturgy'``.
`parse` decides which one the user typed: text only counts as a query when
sqlglot reads it as a standalone condition whose every boolean operand is a
comparison — ``Rob and Ann`` parses, but its operands are bare columns, so
it stays a name search. A shape that *is* a query but cannot run (unknown
field, disallowed construct, bad value) raises `QueryError` from compile;
callers show the message instead of silently falling back.

User text is never executed as SQL. The AST is compiled either into
SQLAlchemy expressions over the as-of entities (volunteers) or into a plain
Python predicate over the teams page's row dicts — always with literals as
bound parameters, so the audit listener and the permission model keep
working. The grammar is deliberately small: AND/OR/NOT over the comparison
operators (=, !=, <, <=, >, >=, LIKE, ILIKE, IN, BETWEEN, IS [NOT] NULL),
fields on one side and literals on the other. No functions, subqueries,
casts, arithmetic, or field-to-field comparisons.

Access control mirrors services.volunteers.search's leak model, extended to
negation. NOT is pushed to the leaves (De Morgan), then each leaf over a
*private* field (phone, notes, custom values) is compiled as
``AND(visible, comparison)`` where ``visible`` is self plus the actor's
full-view teams: a private predicate is *false* for volunteers whose
private fields the actor cannot see — in either polarity, so
``NOT (phone LIKE '555%')`` cannot leak by absence any more than the
positive form can leak by presence. Membership fields (team, role) are
*roster* tier: their EXISTS only considers teams whose roster the actor may
view, and negation applies at the EXISTS (``team != 'X'`` means "holds no
such visible membership"), so invisible memberships never influence the
result either way. Public fields (names, email — which already match for
everyone in plain search — id, created, is_active) compile bare. Admins and
trusted internal callers (actor None) skip the wraps. Tables outside the
registry (accounts, history, ballots…) simply cannot be named.
"""

import re
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as time_lib
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import sqlalchemy as sa
import sqlglot
from sqlalchemy.dialects.postgresql import INTERVAL, UUID
from sqlglot import errors as sqlglot_errors
from sqlglot import exp

from . import fieldcodec
from .models import CustomFieldDef, FieldType, TeamRole
from .permissions import Actor


class QueryError(ValueError):
    """A boolean-shaped query that cannot run; the message is shown as-is."""


_PUBLIC, _PRIVATE, _ROSTER = "public", "private", "roster"

_COMPARISONS = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Like,
    exp.ILike,
    exp.In,
    exp.Between,
    exp.Is,
)

# cheap tell for "might be a query" — plain names skip sqlglot entirely
_QUERYISH = re.compile(
    r"[<>=!]|\b(?:like|ilike|in|between|is|not|and|or)\b", re.IGNORECASE
)


def parse(text: str) -> exp.Expression | None:
    """The parsed condition, or None when the text is a plain substring search.

    Never raises for user text: anything sqlglot cannot read as one
    standalone condition, or whose boolean operands are not comparisons,
    falls back to the substring search.
    """
    text = (text or "").strip()
    if not text or not _QUERYISH.search(text):
        return None
    try:
        ast = sqlglot.parse_one(text, read="postgres", into=exp.Condition)
    except sqlglot_errors.SqlglotError:
        return None
    return ast if _is_condition(ast) else None


def _is_condition(node: exp.Expression) -> bool:
    if isinstance(node, (exp.Not, exp.Paren)):
        return _is_condition(node.this)
    if isinstance(node, (exp.And, exp.Or)):
        return _is_condition(node.this) and _is_condition(node.expression)
    if isinstance(node, exp.In):
        # "Maria in Liturgy" parses as field containment (bare-column RHS);
        # that is prose, not a filter — real IN lists carry `expressions`
        return not node.args.get("field")
    return isinstance(node, _COMPARISONS)


def _compile(node: exp.Expression, negate: bool, backend) -> Any:
    """NNF walk: push NOT to the leaves so tier scoping is sound under negation."""
    if isinstance(node, exp.Paren):
        return _compile(node.this, negate, backend)
    if isinstance(node, exp.Not):
        return _compile(node.this, not negate, backend)
    if isinstance(node, (exp.And, exp.Or)):
        flip = isinstance(node, exp.And) == negate  # De Morgan under negation
        combine = backend.disj if flip else backend.conj
        return combine(
            _compile(node.this, negate, backend),
            _compile(node.expression, negate, backend),
        )
    return backend.leaf(node, negate)


# --- leaf dissection ---------------------------------------------------------

_BINARY_OPS = {
    exp.EQ: "eq",
    exp.NEQ: "neq",
    exp.GT: "gt",
    exp.GTE: "gte",
    exp.LT: "lt",
    exp.LTE: "lte",
    exp.Like: "like",
    exp.ILike: "ilike",
}
_FLIP = {"eq": "eq", "neq": "neq", "gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte"}


def _leaf_parts(
    node: exp.Expression,
) -> tuple[exp.Column, str, list[exp.Expression], bool]:
    """(field, operator, literal nodes, self-negation) for one comparison.

    sqlglot spells ``NOT LIKE`` / ``IS NOT NULL`` as a negate flag on the
    comparison itself; NOT IN / NOT BETWEEN arrive as exp.Not and are
    handled by the NNF walk.
    """
    self_neg = bool(node.args.get("negate"))
    if isinstance(node, exp.In):
        if node.args.get("query") is not None:
            raise QueryError("subqueries are not supported")
        if not isinstance(node.this, exp.Column):
            raise QueryError("IN needs a field on its left side")
        if not node.expressions:
            raise QueryError("IN needs at least one value")
        return node.this, "in", list(node.expressions), self_neg
    if isinstance(node, exp.Between):
        if not isinstance(node.this, exp.Column):
            raise QueryError("BETWEEN needs a field on its left side")
        return node.this, "between", [node.args["low"], node.args["high"]], self_neg
    if isinstance(node, exp.Is):
        if not isinstance(node.this, exp.Column) or not isinstance(
            node.expression, exp.Null
        ):
            raise QueryError("IS only supports `field IS [NOT] NULL`")
        return node.this, "isnull", [], self_neg
    op = _BINARY_OPS.get(type(node))
    if op is None:
        raise QueryError(f"unsupported syntax: {node.sql(dialect='postgres')}")
    left, right = node.this, node.expression
    if isinstance(left, exp.Column) and isinstance(right, exp.Column):
        raise QueryError("comparing two fields is not supported")
    if isinstance(left, exp.Column):
        return left, op, [right], self_neg
    if isinstance(right, exp.Column) and op in _FLIP:
        return right, _FLIP[op], [left], self_neg
    raise QueryError(f"one side of {op.upper()} must be a field")


def _check_op(op: str, kind: FieldType, name: str) -> None:
    if op in ("like", "ilike") and kind not in (FieldType.text, FieldType.select):
        raise QueryError(f"{name}: LIKE works on text fields only")
    if kind is FieldType.checkbox and op not in ("eq", "neq", "isnull"):
        raise QueryError(f"{name}: true/false fields support = and != only")


def _literal(kind: FieldType, node: exp.Expression, name: str) -> Any:
    """One literal as the Python object to bind for a field of `kind`."""
    negative = False
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        node, negative = node.this, True
    if isinstance(node, exp.Boolean):
        if kind is not FieldType.checkbox:
            raise QueryError(f"{name} cannot be compared to true/false")
        return bool(node.this)
    if not isinstance(node, exp.Literal):
        raise QueryError(
            "only literal values are supported (no functions or expressions)"
        )
    text = str(node.this)
    if node.is_string:
        if negative:
            raise QueryError(f"{name}: cannot negate a quoted value")
        try:
            match kind:
                case FieldType.text | FieldType.select:
                    return text
                case FieldType.date:
                    return date.fromisoformat(text)
                case FieldType.timestamp:
                    dt = datetime.fromisoformat(text)
                    if dt.tzinfo is not None:
                        raise QueryError(
                            f"{name}: timestamps here carry no offset — "
                            "write '2026-08-17 10:30'"
                        )
                    return dt
                case FieldType.timestamptz:
                    dt = datetime.fromisoformat(text)
                    # a bare date or naive timestamp is read as UTC
                    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
                case FieldType.time:
                    t = time_lib.fromisoformat(text)
                    if t.tzinfo is not None:
                        raise QueryError(f"{name}: times carry no offset")
                    return t
                case FieldType.interval:
                    return fieldcodec.parse_duration(text)
                case FieldType.uuid:
                    return uuid_lib.UUID(text)
                case FieldType.checkbox:
                    raise QueryError(f"{name} is compared to true or false, unquoted")
                case FieldType.decimal:
                    return Decimal(text)
                case _:  # number, integer
                    raise QueryError(f"{name}: numbers are written without quotes")
        except (ValueError, InvalidOperation) as err:
            if isinstance(err, QueryError):
                raise
            raise QueryError(f"{name}: {err}") from None
    if negative:
        text = f"-{text}"
    match kind:
        case FieldType.number:
            return int(text) if re.fullmatch(r"-?\d+", text) else float(text)
        case FieldType.integer:
            if not re.fullmatch(r"-?\d+", text):
                raise QueryError(f"{name}: must be a whole number")
            return int(text)
        case FieldType.decimal:
            return Decimal(text)
        case FieldType.checkbox:
            raise QueryError(f"{name} is compared to true or false")
        case _:
            raise QueryError(f"{name}: value must be quoted, like 'example'")


# --- volunteers: compile to SQLAlchemy over the as-of entities ---------------

_CASTS: dict[FieldType, sa.types.TypeEngine | None] = {
    FieldType.text: None,
    FieldType.select: None,
    FieldType.number: sa.Numeric(),
    FieldType.integer: sa.BigInteger(),
    FieldType.decimal: sa.Numeric(),
    FieldType.date: sa.Date(),
    FieldType.timestamp: sa.TIMESTAMP(timezone=False),
    FieldType.timestamptz: sa.TIMESTAMP(timezone=True),
    FieldType.time: sa.Time(),
    FieldType.interval: INTERVAL(),
    FieldType.uuid: UUID(),
    FieldType.checkbox: sa.Boolean(),
}


@dataclass(frozen=True)
class _Field:
    kind: FieldType
    tier: str
    build: Callable  # (_SqlBackend) -> SQL expression


_VOLUNTEER_FIELDS: dict[str, _Field] = {
    "name": _Field(
        FieldType.text, _PUBLIC, lambda b: b.V.first_name + " " + b.V.last_name
    ),
    "first_name": _Field(FieldType.text, _PUBLIC, lambda b: b.V.first_name),
    "last_name": _Field(FieldType.text, _PUBLIC, lambda b: b.V.last_name),
    "email": _Field(FieldType.text, _PUBLIC, lambda b: b.V.email),
    "id": _Field(FieldType.integer, _PUBLIC, lambda b: b.V.id),
    "is_active": _Field(FieldType.checkbox, _PUBLIC, lambda b: b.V.is_active),
    "created": _Field(FieldType.timestamptz, _PUBLIC, lambda b: b.V.created_at),
    "phone": _Field(FieldType.text, _PRIVATE, lambda b: b.V.phone),
    "notes": _Field(FieldType.text, _PRIVATE, lambda b: b.V.notes),
    "team": _Field(FieldType.text, _ROSTER, lambda b: b.T.name),
    "role": _Field(FieldType.text, _ROSTER, lambda b: sa.cast(b.M.role, sa.String())),
}


def _sql_cmp(e, op: str, vals: list, negate: bool):
    match op:
        case "eq":
            return e != vals[0] if negate else e == vals[0]
        case "neq":
            return e == vals[0] if negate else e != vals[0]
        case "gt":
            return e <= vals[0] if negate else e > vals[0]
        case "gte":
            return e < vals[0] if negate else e >= vals[0]
        case "lt":
            return e >= vals[0] if negate else e < vals[0]
        case "lte":
            return e > vals[0] if negate else e <= vals[0]
        case "like":
            return e.not_like(vals[0]) if negate else e.like(vals[0])
        case "ilike":
            return e.not_ilike(vals[0]) if negate else e.ilike(vals[0])
        case "in":
            return e.not_in(vals) if negate else e.in_(vals)
        case "between":
            expr = e.between(vals[0], vals[1])
            return sa.not_(expr) if negate else expr
        case "isnull":
            return e.is_not(None) if negate else e.is_(None)
    raise QueryError(f"unsupported operator: {op}")


class _SqlBackend:
    conj = staticmethod(sa.and_)
    disj = staticmethod(sa.or_)

    def __init__(self, V, M, T, defs: dict[str, CustomFieldDef], actor: Actor | None):
        self.V, self.M, self.T = V, M, T
        self.defs = defs
        self.scoped = actor is not None and not actor.is_admin
        self.actor = actor
        self.roster_scope = (
            actor.full_view_team_ids | actor.names_view_team_ids
            if self.scoped
            else None
        )
        self._visible = None

    def _resolve(self, col: exp.Column) -> tuple[str, _Field]:
        if col.args.get("db") or col.args.get("catalog"):
            raise QueryError(f"unknown field: {col.sql(dialect='postgres')}")
        table, name = (col.table or "").lower(), col.name.lower()
        if table and table != "custom":
            raise QueryError(
                f"unknown qualifier: {table} (custom fields are custom.<key>)"
            )
        defn = self.defs.get(name)
        if not table and name in _VOLUNTEER_FIELDS:
            return name, _VOLUNTEER_FIELDS[name]
        if defn is not None:
            kind = FieldType(defn.field_type)
            return name, _Field(
                kind,
                _PRIVATE,
                lambda b, key=name, k=kind: (
                    sa.cast(b.V.custom[key].astext, _CASTS[k])
                    if _CASTS[k] is not None
                    else b.V.custom[key].astext
                ),
            )
        known = sorted(_VOLUNTEER_FIELDS) + sorted(f"custom.{k}" for k in self.defs)
        raise QueryError(f"unknown field: {name} (fields: {', '.join(known)})")

    def _visible_pred(self):
        """Self plus full-view teams — services.volunteers.search's scope."""
        if self._visible is None:
            preds = []
            if self.actor.volunteer_id is not None:
                preds.append(self.V.id == self.actor.volunteer_id)
            if self.actor.full_view_team_ids:
                preds.append(
                    sa.exists(
                        sa.select(sa.literal(1)).where(
                            self.M.volunteer_id == self.V.id,
                            self.M.team_id.in_(self.actor.full_view_team_ids),
                        )
                    )
                )
            self._visible = sa.or_(*preds) if preds else sa.false()
        return self._visible

    def _roster_leaf(self, name: str, spec: _Field, op, vals, negate: bool):
        where = [self.M.volunteer_id == self.V.id]
        if name == "team":
            where.append(self.T.id == self.M.team_id)
        if self.roster_scope is not None:
            where.append(self.M.team_id.in_(self.roster_scope))
        if op == "isnull":
            # team IS NULL: no (visible) membership at all
            exists = sa.exists(sa.select(sa.literal(1)).where(*where))
            return exists if negate else sa.not_(exists)
        # the comparison stays positive inside EXISTS; != and NOT negate the
        # EXISTS itself ("holds no such visible membership"), which cannot
        # leak because invisible memberships never influence either polarity
        inner_op, flip = ("eq", True) if op == "neq" else (op, False)
        where.append(_sql_cmp(spec.build(self), inner_op, vals, False))
        exists = sa.exists(sa.select(sa.literal(1)).where(*where))
        return sa.not_(exists) if (negate ^ flip) else exists

    def leaf(self, node: exp.Expression, negate: bool):
        col, op, lit_nodes, self_neg = _leaf_parts(node)
        negate ^= self_neg
        name, spec = self._resolve(col)
        _check_op(op, spec.kind, name)
        vals = [_literal(spec.kind, n, name) for n in lit_nodes]
        if name == "role":
            allowed = [r.value for r in TeamRole]
            for v in vals:
                if v not in allowed:
                    raise QueryError(f"role must be one of: {', '.join(allowed)}")
        if spec.tier is _ROSTER:
            return self._roster_leaf(name, spec, op, vals, negate)
        expr = _sql_cmp(spec.build(self), op, vals, negate)
        if spec.tier is _PRIVATE and self.scoped:
            return sa.and_(self._visible_pred(), expr)
        return expr


def compile_volunteers(
    ast: exp.Expression,
    *,
    V,
    M,
    T,
    defs: dict[str, CustomFieldDef],
    actor: Actor | None,
):
    """A SQLAlchemy predicate over the as-of entities. Raises QueryError."""
    return _compile(ast, False, _SqlBackend(V, M, T, defs, actor))


# --- teams: compile to a Python predicate over the page's row dicts ----------


def _text_of(key: str) -> Callable[[dict], str | None]:
    return lambda row: row.get(key) or None  # empty string reads as NULL


def _count_of(key: str) -> Callable[[dict], int | None]:
    # counts are blanked ("") server-side for teams the actor does not
    # manage; None makes every comparison false, reproducing that gate
    return lambda row: None if row.get(key) == "" else row.get(key)


_TEAM_FIELDS: dict[str, tuple[FieldType, Callable[[dict], Any]]] = {
    "name": (FieldType.text, _text_of("name")),
    "path": (FieldType.text, _text_of("path")),
    "description": (FieldType.text, _text_of("description")),
    "is_active": (FieldType.checkbox, lambda row: not row.get("inactive")),
    "leaders": (FieldType.integer, _count_of("leader")),
    "seconds": (FieldType.integer, _count_of("second")),
    "core": (FieldType.integer, _count_of("core")),
    "members": (FieldType.integer, _count_of("member")),
    "total": (FieldType.integer, _count_of("total")),
    "gaps": (FieldType.integer, _count_of("gaps")),
}


def _like_regex(pattern: str, ignore_case: bool) -> re.Pattern:
    parts = [
        ".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern
    ]
    flags = re.DOTALL | (re.IGNORECASE if ignore_case else 0)
    return re.compile("".join(parts), flags)


def _py_cmp(getter: Callable, op: str, vals: list, negate: bool) -> Callable:
    pattern = _like_regex(vals[0], op == "ilike") if op in ("like", "ilike") else None

    def test(row: dict) -> bool:
        v = getter(row)
        if op == "isnull":
            return (v is not None) if negate else (v is None)
        if v is None:
            return False  # SQL three-valued logic: NULL never matches
        match op:
            case "eq":
                res = v == vals[0]
            case "neq":
                res = v != vals[0]
            case "gt":
                res = v > vals[0]
            case "gte":
                res = v >= vals[0]
            case "lt":
                res = v < vals[0]
            case "lte":
                res = v <= vals[0]
            case "like" | "ilike":
                res = pattern.fullmatch(v) is not None
            case "in":
                res = v in vals
            case "between":
                res = vals[0] <= v <= vals[1]
            case _:
                raise QueryError(f"unsupported operator: {op}")
        return (not res) if negate else res

    return test


class _PyBackend:
    conj = staticmethod(lambda a, b: lambda row: a(row) and b(row))
    disj = staticmethod(lambda a, b: lambda row: a(row) or b(row))

    def leaf(self, node: exp.Expression, negate: bool) -> Callable[[dict], bool]:
        col, op, lit_nodes, self_neg = _leaf_parts(node)
        negate ^= self_neg
        if col.args.get("db") or col.args.get("catalog") or col.table:
            raise QueryError(f"unknown field: {col.sql(dialect='postgres')}")
        name = col.name.lower()
        if name not in _TEAM_FIELDS:
            raise QueryError(
                f"unknown field: {name} (fields: {', '.join(sorted(_TEAM_FIELDS))})"
            )
        kind, getter = _TEAM_FIELDS[name]
        _check_op(op, kind, name)
        vals = [_literal(kind, n, name) for n in lit_nodes]
        return _py_cmp(getter, op, vals, negate)


def compile_teams(ast: exp.Expression) -> Callable[[dict], bool]:
    """A Python predicate over teams-page row dicts. Raises QueryError.

    Teams are globally readable, so there is no actor scoping here; the
    coverage counts are already blanked server-side for unmanaged teams,
    and the blank reads as NULL (comparisons never match it).
    """
    return _compile(ast, False, _PyBackend())
