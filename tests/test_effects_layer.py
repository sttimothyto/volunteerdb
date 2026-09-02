"""The effects sweep: mail, audit lines and throttle charges leave the edges.

A service returns domain events; policy.plan turns them into effects; ONE
interpreter (effects.run) performs them after the commit. So under api/, ui/
and services/ nothing sends mail, writes an audit line or touches a throttle
directly. BASELINE is what the tree carried when the sweep was written, per
module and per name; a count may only fall, and an entry that reached zero
has to be deleted. Regenerate with `uv run python tests/test_effects_layer.py`.

Reading the ledger is fine -- `env.throttle.snapshot()` is how an edge hands
the policy (or its own pre-check, through pure throttle.blocked) a value;
charging it is not: a hit is a ThrottleHit effect the interpreter performs."""

import ast
import pathlib
from collections import Counter

import pytest

pytestmark = pytest.mark.pure

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "volunteerdb"
EDGES = ("api", "ui", "services")

# Call names that perform an effect in place: the audit line, and the
# throttle cell's methods.
FORBIDDEN_CALLS = frozenset({"audit_log"})
# `<something>.throttle.<method>(...)`: the mutable cell reached directly.
# snapshot() is the read that feeds the policy and is not counted.
THROTTLE_ATTR = "throttle"
THROTTLE_READS = frozenset({"snapshot"})

# What the tree carried when the sweep was written: module -> name -> count.
BASELINE: dict[str, dict[str, int]] = {
    "services/roster_sheets.py": {"audit_log": 2},
}


def _core_files() -> list[pathlib.Path]:
    return sorted(p for edge in EDGES for p in (SRC / edge).rglob("*.py"))


def _violations(path: pathlib.Path) -> Counter[str]:
    """Per forbidden name, how many call sites this module has."""
    tree = ast.parse(path.read_text())
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name in FORBIDDEN_CALLS:
            found[name] += 1
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == THROTTLE_ATTR
            and name not in THROTTLE_READS
        ):
            found["throttle"] += 1
    return found


def test_effects_leave_the_edges_only_through_the_interpreter():
    grown, stale = [], []
    for path in _core_files():
        rel = str(path.relative_to(SRC))
        found = _violations(path)
        allowed = BASELINE.get(rel, {})
        for name, n in found.items():
            if n > allowed.get(name, 0):
                grown.append(f"{rel}: {name} x{n} (baseline {allowed.get(name, 0)})")
        for name, n in allowed.items():
            if found.get(name, 0) == 0:
                stale.append(f"{rel}: {name}")
    assert not grown, (
        "an effect is performed in place here; return a domain event and let "
        f"policy.plan / effects.run do it after the commit: {grown}"
    )
    assert not stale, f"delete these BASELINE entries: {stale}"


def test_the_baseline_only_names_real_modules():
    for rel in BASELINE:
        assert (SRC / rel).exists(), rel


if __name__ == "__main__":  # print a fresh baseline
    fresh = {
        str(p.relative_to(SRC)): dict(sorted(v.items()))
        for p in _core_files()
        if (v := _violations(p))
    }
    print("BASELINE: dict[str, dict[str, int]] = {")
    for rel, counts in sorted(fresh.items()):
        print(f"    {rel!r}: {counts!r},")
    print("}")
