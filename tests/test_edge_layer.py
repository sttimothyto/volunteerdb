"""The edge sweep: what a page or a route reaches for that its context holds.

docs/explanation/architecture.md gives every request one context -- the
API's ``Ctx``, a page's ``PageCtx`` -- carrying the session, the actor, the
Env, the moment and the origin. Three habits work around it, and each one is
a small reason the next reader has to hold more of the tree in their head:

- reading the process Env (``current_env()``) from inside a page that was
  handed a context, so the clock or the zone is read twice in one request;
- querying the ORM from a router or a page (``session.get``, ``sa.select``),
  which puts a read beside the services that own the rest of that table;
- re-deriving the request's origin or client address from the raw request,
  seventeen times over, with two spellings of the fallback.

Modelled on the other three sweeps and, like them, self-maintaining: the
BASELINE is what the tree carried when the sweep was written, per module and
per habit. A count may only fall, and an entry that reached zero has to be
deleted. Regenerate a fresh baseline with
``uv run python tests/test_edge_layer.py``.
"""

import ast
import pathlib
from collections import Counter

import pytest

pytestmark = pytest.mark.pure

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "volunteerdb"
EDGES = ("api", "ui")

# The two modules that ARE the edge kernel: api/deps builds the context and
# ui/context builds on it, so their reads are the ones every other module is
# meant to go through.
KERNEL = frozenset({"api/deps.py", "ui/context.py"})

ENV_READ = "env_read"  # current_env() / current() / env.current()
ORM_REACH = "orm_reach"  # session.<get|execute|scalar|scalars|add|delete>, sa.select
REQUEST_FACTS = "request_facts"  # request.base_url, request.client

_ENV_READERS = frozenset({"current_env", "current"})
_SESSION_METHODS = frozenset({"get", "execute", "scalar", "scalars", "add", "delete"})
_REQUEST_ATTRS = frozenset({"base_url", "client"})

# What the tree carried when the sweep was written: module -> habit -> count.
BASELINE: dict[str, dict[str, int]] = {
    "api/events.py": {"orm_reach": 4},
    "ui/account_page.py": {"env_read": 4},
    "ui/account_status.py": {"env_read": 2},
    "ui/calendar_panel.py": {"env_read": 1},
    "ui/calendar_routes.py": {"env_read": 1},
    "ui/dashboard.py": {"env_read": 2},
    "ui/elections_page.py": {"env_read": 6},
    "ui/events_page.py": {"env_read": 3},
    "ui/invites.py": {"env_read": 2},
    "ui/layout.py": {"env_read": 1},
    "ui/login.py": {"env_read": 3},
    "ui/logo_route.py": {"env_read": 2, "orm_reach": 2},
    "ui/ministries_routes.py": {"env_read": 3, "orm_reach": 2},
    "ui/photos_route.py": {"env_read": 1},
    "ui/team_files_route.py": {"env_read": 5, "orm_reach": 1},
    "ui/teams_page.py": {"env_read": 4, "orm_reach": 2},
    "ui/volunteer_panel.py": {"env_read": 1},
    "ui/volunteers_page.py": {"env_read": 1},
}


def _edge_files() -> list[pathlib.Path]:
    return sorted(p for edge in EDGES for p in (SRC / edge).rglob("*.py"))


def _is_env_read(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _ENV_READERS
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "current"
        and isinstance(func.value, ast.Name)
        and func.value.id in {"env", "env_mod"}
    )


def _is_orm_reach(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    owner = func.value
    if func.attr == "select" and isinstance(owner, ast.Name) and owner.id == "sa":
        return True
    if func.attr not in _SESSION_METHODS:
        return False
    # session.get(...) and ctx.session.get(...) alike
    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    return name == "session"


def _is_request_fact(node: ast.Attribute) -> bool:
    return (
        node.attr in _REQUEST_ATTRS
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
    )


def _violations(path: pathlib.Path) -> Counter[str]:
    found: Counter[str] = Counter()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            if _is_env_read(node):
                found[ENV_READ] += 1
            if _is_orm_reach(node):
                found[ORM_REACH] += 1
        elif isinstance(node, ast.Attribute) and _is_request_fact(node):
            found[REQUEST_FACTS] += 1
    return found


def current() -> dict[str, dict[str, int]]:
    return {
        str(p.relative_to(SRC)): dict(sorted(c.items()))
        for p in _edge_files()
        if str(p.relative_to(SRC)) not in KERNEL and (c := _violations(p))
    }


def test_the_edges_reach_past_their_context_no_more_than_before():
    grown, stale = [], []
    now = current()
    for module, found in now.items():
        allowed = BASELINE.get(module, {})
        for habit, n in found.items():
            if n > allowed.get(habit, 0):
                grown.append(
                    f"{module}: {habit} x{n} (baseline {allowed.get(habit, 0)})"
                )
    for module, habits in BASELINE.items():
        for habit in habits:
            if now.get(module, {}).get(habit, 0) == 0:
                stale.append(f"{module}: {habit}")
    assert not grown, (
        "a page or route reaches past its context here. The Env, the clock, the "
        "origin and the client address are on the Ctx/PageCtx; a read of a table "
        f"belongs in the service that owns it: {grown}"
    )
    assert not stale, (
        f"delete these BASELINE entries, the code no longer needs them: {stale}"
    )


def test_the_baseline_only_names_real_modules():
    for rel in BASELINE:
        assert (SRC / rel).exists(), rel


if __name__ == "__main__":  # pragma: no cover - baseline printer
    print("BASELINE: dict[str, dict[str, int]] = {")
    for rel, counts in sorted(current().items()):
        print(f"    {rel!r}: {counts!r},")
    print("}")
