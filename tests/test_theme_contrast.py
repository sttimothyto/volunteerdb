"""The colour pairs the theme relies on, held to WCAG 2.2 AA.

Read straight from theme.css and main.py, so a re-tint that drops a pair
under the line fails here rather than in somebody's eyes. The ratios are
WCAG's own (relative luminance, 1.4.3 / 1.4.11): 4.5:1 for text, 3:1 for
large text and for the parts of a control that have to be seen — a field's
border, a focus ring, an edge in the graph.

docs/explanation/accessibility.md carries the measured table these
assertions came from.
"""

import re
from pathlib import Path

import pytest

from volunteerdb.services import workload

ROOT = Path(__file__).resolve().parents[1] / "src" / "volunteerdb"
THEME = (ROOT / "ui" / "static" / "theme.css").read_text()
MAIN = (ROOT / "main.py").read_text()

TEXT = 4.5
LARGE = 3.0
NON_TEXT = 3.0


def _luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _block(selector: str) -> dict[str, str]:
    """`--name: #hex` tokens inside the first `selector { ... }` block."""
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", THEME, re.S)
    assert match, f"{selector} block not found in theme.css"
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", match.group(1)))


def _brand() -> dict[str, str]:
    match = re.search(r"app\.colors\((.*?)\)", MAIN, re.S)
    assert match, "app.colors(...) not found in main.py"
    return dict(re.findall(r'(\w+)="(#[0-9a-fA-F]{6})"', match.group(1)))


LIGHT = _block(":root")
DARK = _block("body.body--dark")
BRAND = _brand()
INK_ON_DARK_FILLS = "#1c1917"  # theme.css: dark-mode fills carry ink labels


def _remap(cls: str, dark: bool = False) -> str:
    prefix = "body.body--dark " if dark else ""
    match = re.search(
        re.escape(f"{prefix}.{cls}") + r"\s*\{\s*color:\s*(#[0-9a-fA-F]{6})", THEME
    )
    assert match, f"no {'dark ' if dark else ''}remap for .{cls} in theme.css"
    return match.group(1)


@pytest.mark.parametrize(
    "fg, bg, floor, what",
    [
        ("--vdb-ink", "--vdb-bg", TEXT, "body text"),
        ("--vdb-ink", "--vdb-surface", TEXT, "body text on a card"),
        ("--vdb-heading", "--vdb-bg", TEXT, "table headers, captions (13px)"),
        ("--vdb-heading", "--vdb-surface", TEXT, "headings on a card"),
        ("--vdb-link", "--vdb-bg", TEXT, "links"),
        ("--vdb-link", "--vdb-surface", TEXT, "links on a card"),
        ("--vdb-warn", "--vdb-bg", TEXT, "flagged figures"),
        ("--vdb-muted", "--vdb-bg", TEXT, "secondary text"),
        ("--vdb-muted", "--vdb-surface", TEXT, "secondary text on a card"),
        ("--vdb-muted", "--vdb-row-hover", TEXT, "secondary text on a hovered row"),
        ("--vdb-heading", "--vdb-bg", NON_TEXT, "the focus ring"),
        ("--vdb-graph-edge", "--vdb-bg", NON_TEXT, "graph edges"),
        ("--vdb-graph-leader", "--vdb-bg", NON_TEXT, "leader nodes"),
        ("--vdb-graph-label", "--vdb-graph-node", TEXT, "node labels"),
        ("--vdb-graph-team-label", "--vdb-graph-team", TEXT, "team labels"),
    ],
)
def test_light_tokens(fg, bg, floor, what):
    ratio = contrast(LIGHT[fg], LIGHT[bg])
    assert ratio >= floor, f"{what}: {fg} on {bg} is {ratio:.2f}:1, needs {floor}:1"


@pytest.mark.parametrize(
    "fg, bg, floor, what",
    [
        ("--vdb-ink", "--vdb-bg", TEXT, "body text"),
        ("--vdb-ink", "--vdb-surface", TEXT, "body text on a card"),
        ("--vdb-heading", "--vdb-bg", TEXT, "headings"),
        ("--vdb-heading", "--vdb-surface", TEXT, "headings on a card"),
        ("--vdb-link", "--vdb-bg", TEXT, "links"),
        ("--vdb-warn", "--vdb-bg", TEXT, "flagged figures"),
        ("--vdb-muted", "--vdb-bg", TEXT, "secondary text"),
        ("--vdb-muted", "--vdb-surface", TEXT, "secondary text on a card"),
        ("--vdb-graph-edge", "--vdb-bg", NON_TEXT, "graph edges"),
        ("--vdb-graph-label", "--vdb-graph-node", TEXT, "node labels"),
    ],
)
def test_dark_tokens(fg, bg, floor, what):
    ratio = contrast(DARK[fg], DARK[bg])
    assert ratio >= floor, f"{what}: {fg} on {bg} is {ratio:.2f}:1, needs {floor}:1"


def test_the_header_reads():
    assert contrast("#ffffff", LIGHT["--vdb-header-bg"]) >= TEXT
    assert contrast("#ffffff", DARK["--vdb-header-bg"]) >= TEXT
    assert contrast(LIGHT["--vdb-accent"], LIGHT["--vdb-header-bg"]) >= NON_TEXT, (
        "the double rule under the header"
    )


@pytest.mark.parametrize(
    "name", ["primary", "secondary", "positive", "negative", "warning", "info", "muted"]
)
def test_light_brand_colours_work_as_fill_and_as_text(name):
    """A brand colour is painted two ways: as a button or badge with white
    text on it, and as text on the page (text-negative, a flat button)."""
    colour = BRAND[name]
    assert contrast("#ffffff", colour) >= TEXT, f"white on {name} {colour}"
    assert contrast(colour, LIGHT["--vdb-bg"]) >= TEXT, f"{name} as text on the page"
    assert contrast(colour, LIGHT["--vdb-surface"]) >= TEXT, f"{name} as text on a card"


@pytest.mark.parametrize(
    "name", ["primary", "secondary", "positive", "negative", "warning", "info", "muted"]
)
def test_dark_brand_colours_work_as_fill_and_as_text(name):
    """In dark mode the same colour must read as text on the dark page AND
    carry an ink label as a fill (theme.css gives dark fills ink text)."""
    colour = DARK[f"--q-{name}"]
    assert contrast(INK_ON_DARK_FILLS, colour) >= TEXT, f"ink on {name} {colour}"
    assert contrast(colour, DARK["--vdb-bg"]) >= TEXT, f"{name} as text on the page"
    assert contrast(colour, DARK["--vdb-surface"]) >= TEXT, f"{name} as text on a card"


@pytest.mark.parametrize("cls", ["text-gray-400", "text-gray-500"])
def test_the_tailwind_greys_are_remapped_on_both_grounds(cls):
    """Tailwind's own greys were chosen for white; on parchment gray-500 is
    4.48:1 and gray-400 2.35:1. The remaps are what the 90-odd call sites
    actually render."""
    assert contrast(_remap(cls), LIGHT["--vdb-bg"]) >= TEXT
    assert contrast(_remap(cls), LIGHT["--vdb-surface"]) >= TEXT
    assert contrast(_remap(cls, dark=True), DARK["--vdb-bg"]) >= TEXT
    assert contrast(_remap(cls, dark=True), DARK["--vdb-surface"]) >= TEXT


def test_outlined_fields_have_a_visible_border():
    match = re.search(
        r"\.q-field--outlined \.q-field__control:before\s*\{[^}]*border-color:\s*(#[0-9a-fA-F]{6})",
        THEME,
    )
    assert match, "theme.css must set the outlined field border (Quasar's is 1.9:1)"
    assert contrast(match.group(1), LIGHT["--vdb-bg"]) >= NON_TEXT


def test_workload_defaults_are_legible_with_their_computed_label():
    for band in workload.DEFAULT_CONFIG.bands:
        label = workload.text_colour(band.color)
        assert contrast(label, band.color) >= TEXT, (
            f"band {band.label} {band.color}: {label} gives "
            f"{contrast(label, band.color):.2f}:1"
        )


def test_text_colour_picks_the_darker_or_lighter_label():
    assert workload.text_colour("#ffb300") == workload.INK
    assert workload.text_colour("#1a237e") == "#ffffff"
    assert workload.contrast_with_label("#ffb300") == pytest.approx(9.74, abs=0.05)
