"""The GUI holds no Python-side state and renders nothing inside a session.

The GUI rule of docs/explanation/architecture.md, as an AST sweep over ui/: a page loads
inside a `page_ctx()` block and renders after it, so nothing under `ui.*`
sits inside one; a nested handler captures values, never a cell -- no
`nonlocal`, no `some_dict[...] = ...` on a name the handler did not bind;
and the transition helpers (notify_errors, action_session, page_session)
are gone. The widgets and the URL are the only state."""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.pure

UI = pathlib.Path(__file__).resolve().parent.parent / "src" / "volunteerdb" / "ui"
SESSION_BLOCKS = frozenset({"page_ctx", "transaction"})
GONE = frozenset({"notify_errors", "action_session", "page_session"})


def _session_blocks(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    name = (
                        call.func.id
                        if isinstance(call.func, ast.Name)
                        else call.func.attr
                        if isinstance(call.func, ast.Attribute)
                        else None
                    )
                    if name in SESSION_BLOCKS:
                        yield node


def _ui_calls(node: ast.AST) -> list[int]:
    lines = []
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "ui"
        ):
            lines.append(inner.lineno)
    return lines


def _bound_names(fn: ast.AST) -> set[str]:
    """Names a function binds itself: parameters and assignment targets."""
    names = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    if fn.args.vararg:
        names.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        names.add(fn.args.kwarg.arg)
    for inner in ast.walk(fn):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
            names.add(inner.id)
        if isinstance(inner, (ast.For, ast.AsyncFor, ast.comprehension)):
            target = inner.target
            names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
    return names


def _free_variable_stores(tree: ast.AST) -> list[str]:
    """`name[...] = ...` (or augmented) inside a nested function on a name
    that function did not bind: a closure mutating its parent's container."""
    found = []
    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for fn in functions:
        bound = _bound_names(fn)
        for inner in ast.walk(fn):
            targets = []
            if isinstance(inner, ast.Assign):
                targets = inner.targets
            elif isinstance(inner, (ast.AugAssign, ast.AnnAssign)):
                targets = [inner.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                    if t.value.id not in bound and t.value.id != "self":
                        found.append(
                            f"{fn.name}:{inner.lineno} {t.value.id}[...] = ..."
                        )
    return found


def test_nothing_renders_inside_a_session():
    stray = []
    for path in sorted(UI.glob("*.py")):
        tree = ast.parse(path.read_text())
        for block in _session_blocks(tree):
            for line in _ui_calls(block):
                stray.append(f"{path.name}:{line}")
    assert not stray, (
        "a ui.* call inside a session block: load the values there and render "
        f"after it, so the transaction never spans a render: {stray}"
    )


def test_no_nonlocal_and_no_closure_mutates_a_container():
    cells, stores = [], []
    for path in sorted(UI.glob("*.py")):
        tree = ast.parse(path.read_text())
        cells += [
            f"{path.name}:{n.lineno}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Nonlocal)
        ]
        stores += [f"{path.name} {s}" for s in _free_variable_stores(tree)]
    assert not cells, f"nonlocal is a mutable cell; capture a value instead: {cells}"
    assert not stores, (
        "a nested handler writes into its parent's container; the widgets and "
        f"the URL are the only state: {stores}"
    )


def test_the_transition_helpers_are_gone():
    seen = []
    for path in sorted(UI.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name) and node.id in GONE:
                seen.append(f"{path.name}:{node.lineno} {node.id}")
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in GONE:
                        seen.append(f"{path.name}:{node.lineno} import {alias.name}")
    assert not seen, f"page_ctx() and run_command() are the whole vocabulary: {seen}"
