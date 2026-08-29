"""Where side effects live, enforced structurally.

FUNCTIONAL_REFACTORING.md states the rules these tests defend: under the core
paths -- services/, sheets/, and the pure leaves beside them -- nothing reads
the clock, the settings or a random source, opens its own transaction, builds
an HTTP client or prints, and nothing raises: time, identifiers and
configuration arrive as parameters, and a refusal is a returned Err.

Modelled on test_authorization_layer.py, and like it self-maintaining: the
BASELINE below is the count of every violation the tree carried when the
sweep was written, per module and per name. A count may only fall, and an
entry that has fallen to zero has to be deleted -- so the list shrinks phase
by phase and can never quietly grow back. Regenerate a fresh baseline with
`uv run python tests/test_purity_layer.py`.
"""

import ast
import pathlib
from collections import Counter

import pytest

pytestmark = pytest.mark.pure

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "volunteerdb"

# The core: business rules and the pure leaves they compose. Edges (api/, ui/,
# jobs/, main.py, scheduler.py, env.py, db.py) are where effects belong.
CORE = (
    "services",
    "sheets",
    "query_lang.py",
    "passwords.py",
    "permissions.py",
    "fieldcodec.py",
    "star.py",
    "history.py",
    "policy.py",
    "domain.py",
    "fp.py",
    "errors.py",
)

# Call names that read the world or write to it from inside the core.
FORBIDDEN_CALLS = frozenset(
    {
        "settings",  # configuration is a parameter
        "now",  # datetime.now and sa.func.now: the clock is a parameter
        "utcnow",
        "today",  # date.today
        "monotonic",
        "perf_counter",
        "token_urlsafe",  # secrets.*: identifiers are parameters
        "token_hex",
        "randbelow",
        "uuid4",
        "db_session",  # the unit of work belongs to the edge
        "sessionmaker",
        "AsyncClient",  # transports are injected
        "print",
    }
)
RAISE = "raise"  # a refusal is a returned Err, never an exception

# What the tree carried when the sweep was written: module -> name -> count.
# Shrinks per phase; a stale entry fails test_the_baseline_has_no_stale_entries.
BASELINE: dict[str, dict[str, int]] = {
    "fieldcodec.py": {"raise": 19},
    "fp.py": {"raise": 3},
    "passwords.py": {"raise": 7, "settings": 1},
    "permissions.py": {"raise": 1},
    "query_lang.py": {"raise": 30},
    "services/branding.py": {"now": 1, "raise": 3},
    "services/custom_fields.py": {"raise": 12},
    "services/elections.py": {"now": 5, "raise": 17, "settings": 1},
    "services/events.py": {"now": 8, "raise": 52, "settings": 2, "uuid4": 1},
    "services/gcal.py": {"AsyncClient": 1, "now": 2, "raise": 10, "settings": 4},
    "services/google_api.py": {"AsyncClient": 1, "raise": 3, "settings": 2},
    "services/gsheets.py": {"AsyncClient": 5, "raise": 13, "settings": 2},
    "services/ics.py": {"now": 1, "settings": 1},
    "services/mail.py": {"AsyncClient": 1, "print": 1, "settings": 4},
    "services/mail_quota.py": {"db_session": 1, "now": 1, "settings": 2},
    "services/memberships.py": {"raise": 1},
    "services/pages.py": {"now": 1, "raise": 9},
    "services/photos.py": {"now": 2, "raise": 5},
    "services/roster_sheets.py": {
        "db_session": 7,
        "now": 1,
        "raise": 3,
        "settings": 1,
        "token_urlsafe": 1,
    },
    "services/stats.py": {"now": 1},
    "services/task_force.py": {"now": 2, "raise": 9, "settings": 1},
    "services/teams.py": {"raise": 8},
    "services/users.py": {"now": 9, "raise": 23, "settings": 1},
    "services/volunteers.py": {"raise": 2, "settings": 1},
    "services/workload.py": {"now": 1, "raise": 10},
    "sheets/common.py": {"raise": 1},
    "sheets/exporter.py": {"raise": 1},
    "sheets/importer.py": {"db_session": 2, "raise": 2},
}


def _core_files():
    for entry in CORE:
        path = SRC / entry
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        elif path.exists():
            yield path


def _module_key(path: pathlib.Path) -> str:
    return str(path.relative_to(SRC))


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_assertion_error(node: ast.Raise) -> bool:
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "AssertionError"


def counts(path: pathlib.Path) -> Counter[str]:
    found: Counter[str] = Counter()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in FORBIDDEN_CALLS:
                found[name] += 1
        elif isinstance(node, ast.Raise) and not _is_assertion_error(node):
            found[RAISE] += 1
    return found


def current() -> dict[str, dict[str, int]]:
    return {
        _module_key(path): dict(sorted(c.items()))
        for path in _core_files()
        if (c := counts(path))
    }


def test_core_impurity_never_grows():
    """Every forbidden call and every raise under the core is either gone or
    still on the baseline at no more than its original count."""
    grown = []
    for module, found in current().items():
        allowed = BASELINE.get(module, {})
        for name, n in found.items():
            if n > allowed.get(name, 0):
                grown.append(f"{module}: {name} x{n} (baseline {allowed.get(name, 0)})")
    assert not grown, (
        "new side effects or raises inside the core. Time, config and identifiers "
        "are parameters; a refusal is a returned Err; the unit of work belongs to "
        f"the edge (FUNCTIONAL_REFACTORING.md, Rules 1-3): {grown}"
    )


def test_the_baseline_has_no_stale_entries():
    """The ratchet only turns one way: a violation that was removed must also
    leave the baseline, so nobody can reintroduce it under an old allowance."""
    now = current()
    stale = [
        f"{module}: {name}"
        for module, names in BASELINE.items()
        for name in names
        if now.get(module, {}).get(name, 0) == 0
    ]
    assert not stale, (
        f"delete these BASELINE entries, the code no longer needs them: {stale}"
    )


if __name__ == "__main__":  # pragma: no cover - baseline printer
    import pprint

    pprint.pprint(current(), width=100, sort_dicts=True)
