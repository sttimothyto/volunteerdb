"""CSS mistakes that render perfectly and still come out wrong.

Tailwind utilities that Quasar's stylesheet silently overrules.

NiceGUI serves Quasar and Tailwind side by side, and quasar.important.css ships
`.hidden{display:none!important}`. Tailwind's `hidden` is the same class name but plain
`display:none`, so the two look interchangeable and are not: the responsive idiom
`hidden md:flex` renders as `display:none!important` at *every* width, because `md:flex`
is a normal declaration and cannot beat !important. That is how the header nav went
invisible on desktop (fixed in layout.py by switching to Quasar's gt-sm/lt-md helpers).

Nothing in a headless render catches this — the element and its classes are present and
correct in the element tree; only the browser cascade hides it. So this reads the source.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "volunteerdb" / "ui"
THEME_CSS = UI_DIR / "static" / "theme.css"

_DISPLAY = "flex|block|inline|inline-flex|inline-block|grid|inline-grid|table|contents|flow-root"

# sm:flex, md:block, lg:inline-flex, 2xl:grid, and their !-important variants
_RESPONSIVE_DISPLAY = re.compile(rf"^(?:sm|md|lg|xl|2xl):!?(?:{_DISPLAY})$")


def _class_literals() -> list[tuple[Path, int, str]]:
    """Every string literal passed positionally to a `.classes(...)` call."""
    found = []
    for path in sorted(UI_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Attribute) and node.func.attr == "classes"
            ):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((path, node.lineno, arg.value))
    return found


def test_no_tailwind_hidden_paired_with_a_responsive_display():
    literals = _class_literals()

    offenders = []
    for path, lineno, classes in literals:
        tokens = classes.split()
        if "hidden" not in tokens:
            continue  # a bare `hidden` toggle (photo_dialog) is fine on its own
        overrides = [t for t in tokens if _RESPONSIVE_DISPLAY.match(t)]
        if overrides:
            offenders.append(
                f"{path.name}:{lineno}: {classes!r} — {' '.join(overrides)}"
            )

    assert not offenders, (
        "Quasar's `.hidden{display:none!important}` beats these Tailwind display utilities, "
        "so the element stays hidden at every width. Use Quasar's breakpoint helpers "
        "(gt-xs/gt-sm/lt-md/…) instead:\n  " + "\n  ".join(offenders)
    )

    # Floor guards against the AST walk silently matching nothing (e.g. if the call sites
    # move off a literal argument), not against legitimately removing a class.
    assert len(literals) >= 50, (
        f"only found {len(literals)} .classes() literals under {UI_DIR} — the AST sweep "
        "stopped seeing the call sites and is no longer covering them"
    )


def test_every_vdb_class_is_defined_in_the_theme():
    """A vdb-* class is ours alone: nothing but theme.css gives it any effect, so a
    typo (or a rename on one side only) is a class that quietly does nothing. The
    element still renders, just unstyled — .vdb-prose without its rule is a page of
    window-wide running text, and no headless assertion notices."""
    defined = set(re.findall(r"\.(vdb-[a-z0-9-]+)", THEME_CSS.read_text()))

    used = {
        (path, lineno, token)
        for path, lineno, classes in _class_literals()
        for token in classes.split()
        if token.startswith("vdb-")
    }
    assert used, "no vdb-* classes found in .classes() literals — the sweep missed them"

    undefined = sorted(
        f"{path.name}:{lineno}: {token}"
        for path, lineno, token in used
        if token not in defined
    )
    assert not undefined, (
        f"these classes are used but never defined in {THEME_CSS.name}, so they style "
        "nothing:\n  " + "\n  ".join(undefined)
    )


def _ui_input_labels() -> list[tuple[Path, int, str]]:
    """The label of every ``ui.input("...")`` call under ui/."""
    found = []
    for path in sorted(UI_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            if node.func.attr != "input" or getattr(node.func.value, "id", "") != "ui":
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                found.append((path, node.lineno, str(node.args[0].value)))
    return found


def test_every_date_or_time_field_offers_a_picker():
    """A field whose label asks for YYYY-MM-DD or HH:MM is a date_input /
    time_input (ui/date_input.py), never a bare ui.input: the picker is what
    keeps the format from being the reader's problem."""
    offenders = [
        f"{path.name}:{line} {label!r}"
        for path, line, label in _ui_input_labels()
        if path.name != "date_input.py" and ("YYYY-MM-DD" in label or "HH:MM" in label)
    ]
    assert not offenders, "date/time fields typed by hand:\n" + "\n".join(offenders)
