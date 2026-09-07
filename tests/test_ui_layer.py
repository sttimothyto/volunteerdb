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


# --- anything that removes a record or takes a person off something asks first ---
#
# The rule of uiux-improvement.md's Decisions, held mechanically: a GUI handler
# that reaches one of these service functions awaits forms.confirm somewhere
# in its own body (the command closure inside it counts as its body), or its
# own dialog is the question. (service module, function), by the name the
# module is imported under in ui/.
REMOVING = frozenset(
    {
        ("teams", "delete"),
        ("memberships", "remove"),
        ("events", "remove_assignment"),
        ("events", "delete_slot"),
        ("events", "cancel_event"),
        ("users", "reissue_invite"),
        ("users", "clear_password"),
        ("photos", "delete_photo"),
        ("branding", "delete_logo"),
        ("volunteers", "delete"),
        ("custom_fields", "delete_def"),
        ("elections", "remove_candidate"),
        ("elections", "remove_voter"),
        ("elections", "cancel"),
    }
)
# Handlers whose own dialog is the question: the reason box a volunteer
# fills in to take themselves off a slot is what they confirm with.
ASKS_WITH_A_DIALOG = frozenset({"events_page.py:_self_removal_dialog"})


def _service_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> services module, for `from ..services import X [as Y]`."""
    aliases: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "services" or node.module.endswith(".services"))
        ):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _removing_calls(fn: ast.AST, aliases: dict[str, str]) -> list[str]:
    found = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (aliases.get(node.func.value.id), node.func.attr) in REMOVING
        ):
            found.append(f"{aliases[node.func.value.id]}.{node.func.attr}")
    return found


def _awaits_confirm(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "confirm":
                return True
    return False


def test_every_removing_action_asks_first():
    unasked = []
    for path in sorted(UI.glob("*.py")):
        tree = ast.parse(path.read_text())
        aliases = _service_aliases(tree)
        if not aliases:
            continue
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _removing_calls(fn, aliases)
            if not calls or f"{path.name}:{fn.name}" in ASKS_WITH_A_DIALOG:
                continue
            if not _awaits_confirm(fn):
                unasked.append(f"{path.name}:{fn.name} calls {', '.join(calls)}")
    assert not unasked, (
        "a handler removes a record, or takes a person off something, without "
        f"`await confirm(...)` (ui/forms.py) in its body: {unasked}"
    )


def test_the_removing_sweep_sees_the_handlers_it_holds():
    """The sweep matches on import aliases; a rename of one would make it
    match nothing and pass. So it has to find the handlers it was written
    for."""
    seen = set()
    for path in sorted(UI.glob("*.py")):
        tree = ast.parse(path.read_text())
        aliases = _service_aliases(tree)
        for fn in tree.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen.update(_removing_calls(fn, aliases))
    assert seen >= {"teams.delete", "memberships.remove", "events.delete_slot"}, seen


# --- every dialog is built by forms.py ---------------------------------------------
#
# dialog_card makes a dialog with fields in it persistent (a click beside it
# is not a decision) and confirm keeps a question dismissible; both decisions
# are site-wide only while nothing else in ui/ calls ui.dialog() itself.


def test_every_dialog_is_built_by_forms():
    stray = []
    for path in sorted(UI.glob("*.py")):
        if path.name == "forms.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dialog"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ui"
            ):
                stray.append(f"{path.name}:{node.lineno}")
    assert not stray, (
        "a ui.dialog() outside forms.py: use dialog_card (persistent, titled) "
        f"or confirm (a question) so every dialog behaves alike: {stray}"
    )


# --- every notification carries a type ------------------------------------------------
#
# context.notify and its four faces (success, info, warn, fail) pass Quasar a
# `type`, which draws the icon: a state told by hue alone is one a colour-
# blind reader cannot tell (WCAG 1.4.1). That holds site-wide only while no
# page calls ui.notify itself -- and a success that precedes a reload goes
# through context.flash, so the reload cannot tear it down.


def test_every_notification_goes_through_context():
    stray = []
    for path in sorted(UI.glob("*.py")):
        if path.name == "context.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "notify"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ui"
            ):
                stray.append(f"{path.name}:{node.lineno}")
    assert not stray, (
        "a ui.notify() outside context.py: use success/info/warn/fail (an icon "
        f"with the colour) or flash (survives the reload): {stray}"
    )
    source = (UI / "context.py").read_text()
    assert "color=" not in source.split("def notify(")[1], (
        "context.notify passes `type`, never `color`: the type is what draws the icon"
    )
