"""Sphinx configuration for the VolunteerDB documentation.

Build:  uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
Links:  uv run --group docs sphinx-build -b linkcheck docs docs/_build/linkcheck
"""

import os
import posixpath
import re
import tomllib
from datetime import date
from pathlib import Path

project = "VolunteerDB"
# Both read at BUILD time, like vdb_contact_email below. The container's docs
# stage takes them as build args from the site file, so the manual an instance
# serves at /manual carries that instance's name.
author = os.environ.get("VDB_ORG_NAME") or "VolunteerDB"
copyright = f"{date.today().year} {author}"
# From pyproject.toml, not importlib.metadata: the container's docs build
# stage runs sphinx via uvx without the volunteerdb package installed.
_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
release = version = tomllib.loads(_pyproject.read_text())["project"]["version"]

extensions = [
    "myst_parser",
    "sphinx_copybutton",
]

myst_enable_extensions = [
    "colon_fence",  # ::: fenced directives (admonitions) in Markdown prose
    "deflist",  # definition lists for config vars and CLI synopses
    "tasklist",  # checklists in runbooks
]
myst_heading_anchors = 3

exclude_patterns = ["_build"]

html_theme = "furo"
html_title = "VolunteerDB"
html_static_path = ["_static"]
html_css_files = ["vdb-theme.css"]
# The audience switch and the live search box (sidebar/*.html below).
html_js_files = ["vdb-manual.js"]
# _templates/page.html appends the report-it footer to Furo's own, sets the
# audience attribute before first paint, and puts the "technical page" notice
# on every page outside the user guide.
templates_path = ["_templates"]
# Furo's own list (its theme.conf), plus the audience switch under the search
# box. html_sidebars replaces the theme's list rather than extending it.
html_sidebars = {
    "**": [
        "sidebar/brand.html",
        "sidebar/search.html",
        "sidebar/audience.html",
        "sidebar/scroll-start.html",
        "sidebar/navigation.html",
        "sidebar/ethical-ads.html",
        "sidebar/scroll-end.html",
        "sidebar/variant-selector.html",
    ]
}
# Read at build time, not runtime: the container bakes this HTML into the
# image, so VDB_CONTACT_EMAIL must be set for the docs build to change it.
html_context = {
    # `or`, not a .get default: the container sets these ARGs to "" when the
    # deploy does not pass them, and an empty address would render as one.
    "vdb_contact_email": os.environ.get("VDB_CONTACT_EMAIL") or "admin@example.invalid",
}

# Neo-greco palette mirroring the web app: tokens from
# src/volunteerdb/ui/static/theme.css, brand colors from app.colors() in
# main.py. Keep the three in sync when re-tinting. Faces and the meander
# ornament live in _static/vdb-theme.css.
html_theme_options = {
    "light_css_variables": {
        # faces (inherited by dark mode)
        "font-stack": 'Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif',
        "font-stack--headings": '"Cinzel", Palatino, "Palatino Linotype", Georgia, serif',
        # ink & parchment
        "color-foreground-primary": "#333333",  # --vdb-ink
        "color-foreground-secondary": "#6b6255",  # muted ink
        "color-foreground-muted": "#6b6255",
        "color-foreground-border": "#d8c9a3",  # --vdb-rule
        "color-background-primary": "#fdf6e3",  # --vdb-bg
        "color-background-secondary": "#fffbee",  # --vdb-surface
        "color-background-hover": "#f5eed7",
        "color-background-hover--transparent": "#f5eed700",
        "color-background-border": "#d8c9a3",
        "color-background-item": "#d8c9a3",
        # brand
        "color-brand-primary": "#a5573e",  # --vdb-heading / q-primary
        "color-brand-content": "#2e5e7e",  # --vdb-link
        "color-brand-visited": "#2e5e7e",  # app links don't shift when visited
        # highlights & code
        "color-highlighted-background": "#d6edff",  # --vdb-selection
        "color-highlight-on-target": "#f5deb3",  # --vdb-accent
        "color-inline-code-background": "#f5eed7",
        "color-code-block-background": "#fffbee",  # consumed by vdb-theme.css
        "color-guilabel-background": "#f5deb380",
        "color-guilabel-border": "#d8c9a380",
        # admonitions on the app's status palette (info/positive/warning/negative)
        "color-admonition-title": "#8a7550",  # q-secondary
        "color-admonition-title-background": "rgba(138, 117, 80, .2)",
        "color-admonition-title--note": "#4c7086",
        "color-admonition-title-background--note": "rgba(76, 112, 134, .2)",
        "color-admonition-title--seealso": "#4c7086",
        "color-admonition-title-background--seealso": "rgba(76, 112, 134, .2)",
        "color-admonition-title--hint": "#5f7a3d",
        "color-admonition-title-background--hint": "rgba(95, 122, 61, .2)",
        "color-admonition-title--tip": "#5f7a3d",
        "color-admonition-title-background--tip": "rgba(95, 122, 61, .2)",
        "color-admonition-title--important": "#5f7a3d",
        "color-admonition-title-background--important": "rgba(95, 122, 61, .2)",
        "color-admonition-title--caution": "#b07d2b",
        "color-admonition-title-background--caution": "rgba(176, 125, 43, .2)",
        "color-admonition-title--warning": "#b07d2b",
        "color-admonition-title-background--warning": "rgba(176, 125, 43, .2)",
        "color-admonition-title--danger": "#9b3b30",
        "color-admonition-title-background--danger": "rgba(155, 59, 48, .2)",
        "color-admonition-title--attention": "#9b3b30",
        "color-admonition-title-background--attention": "rgba(155, 59, 48, .2)",
        "color-admonition-title--error": "#9b3b30",
        "color-admonition-title-background--error": "rgba(155, 59, 48, .2)",
        "color-topic-title": "#8a7550",
        "color-topic-title-background": "rgba(138, 117, 80, .2)",
    },
    "dark_css_variables": {
        # ink & parchment (body.body--dark tokens)
        "color-foreground-primary": "#e8e0ce",
        "color-foreground-secondary": "#a89e8f",
        "color-foreground-muted": "#a89e8f",
        "color-foreground-border": "#4a443c",
        "color-background-primary": "#1c1917",
        "color-background-secondary": "#2a2622",
        "color-background-hover": "#322d28",
        "color-background-hover--transparent": "#322d2800",
        "color-background-border": "#4a443c",
        "color-background-item": "#4a443c",
        # brand
        "color-brand-primary": "#c97b5d",
        "color-brand-content": "#9dc1d9",
        "color-brand-visited": "#9dc1d9",
        # highlights & code
        "color-highlighted-background": "#3a4a5a",
        "color-highlight-on-target": "#33291c",
        "color-inline-code-background": "#322d28",
        "color-code-block-background": "#2a2622",
        "color-guilabel-background": "#3a2e1e80",
        "color-guilabel-border": "#4a443c80",
        # admonitions on the dark status palette
        "color-admonition-title": "#a8916b",
        "color-admonition-title-background": "rgba(168, 145, 107, .2)",
        "color-admonition-title--note": "#6e93a8",
        "color-admonition-title-background--note": "rgba(110, 147, 168, .2)",
        "color-admonition-title--seealso": "#6e93a8",
        "color-admonition-title-background--seealso": "rgba(110, 147, 168, .2)",
        "color-admonition-title--hint": "#7e9c5b",
        "color-admonition-title-background--hint": "rgba(126, 156, 91, .2)",
        "color-admonition-title--tip": "#7e9c5b",
        "color-admonition-title-background--tip": "rgba(126, 156, 91, .2)",
        "color-admonition-title--important": "#7e9c5b",
        "color-admonition-title-background--important": "rgba(126, 156, 91, .2)",
        "color-admonition-title--caution": "#d9a662",
        "color-admonition-title-background--caution": "rgba(217, 166, 98, .2)",
        "color-admonition-title--warning": "#d9a662",
        "color-admonition-title-background--warning": "rgba(217, 166, 98, .2)",
        "color-admonition-title--danger": "#c25b49",
        "color-admonition-title-background--danger": "rgba(194, 91, 73, .2)",
        "color-admonition-title--attention": "#c25b49",
        "color-admonition-title-background--attention": "rgba(194, 91, 73, .2)",
        "color-admonition-title--error": "#c25b49",
        "color-admonition-title-background--error": "rgba(194, 91, 73, .2)",
        "color-topic-title": "#a8916b",
        "color-topic-title-background": "rgba(168, 145, 107, .2)",
    },
}

# Strip shell prompts when copying code blocks.
copybutton_prompt_text = r"\$ |>>> "
copybutton_prompt_is_regexp = True

linkcheck_ignore = [
    r"http://localhost.*",
    r"http://127\.0\.0\.1.*",
    r".*\.example.*",  # seeded demo addresses and the placeholder domain
    r".*\.invalid.*",
]
# A running instance is auth-walled, so linkcheck cannot follow links to it.
if _base_url := os.environ.get("VDB_PUBLIC_BASE_URL"):
    linkcheck_ignore.append(re.escape(_base_url) + ".*")


# ---- the two audiences ------------------------------------------------------
#
# The manual has two halves in one build: the user guide (docs/guide/) and the
# technical pages (everything else). The sidebar shows the guide by default and
# the technical groups only once the reader has said they are a developer
# (localStorage, see _static/vdb-manual.js). Which groups those are is decided
# here, at build time, by where their links point -- one source of truth, the
# guide/ prefix -- so a caption can say whatever reads best.

_CAPTION = '<p class="caption"'
_HREF = re.compile(r'href="([^"#]*)')
_FIRST_UL = re.compile(r"<ul\b[^>]*>")


def _targets_the_guide(group: str, pagename: str) -> bool:
    base = posixpath.dirname(pagename)
    for href in _HREF.findall(group):
        target = (
            pagename if href == "" else posixpath.normpath(posixpath.join(base, href))
        )
        if target.startswith("guide/"):
            return True
    return False


def _hide_from_users(group: str) -> str:
    """Class the caption and the list that follows it, merging with a class
    Sphinx already put there (`current` on the group of the open page)."""

    def mark(match: re.Match[str]) -> str:
        tag = match.group(0)
        if 'class="' in tag:
            return tag.replace('class="', 'class="vdb-dev-only ', 1)
        return tag[:-1] + ' class="vdb-dev-only">'

    return '<p class="caption vdb-dev-only"' + _FIRST_UL.sub(mark, group, count=1)


def _mark_developer_groups(app, pagename, templatename, context, doctree):
    """Runs after Furo's own html-page-context handler (priority 500), which
    is what puts furo_navigation_tree in the context. A group with no link
    into the guide is a technical group. If the key is ever gone, nothing is
    hidden -- a visible failure, never a build warning."""
    tree = context.get("furo_navigation_tree")
    if not tree or _CAPTION not in tree:
        return
    head, *groups = tree.split(_CAPTION)
    marked = [
        _CAPTION + group
        if _targets_the_guide(group, pagename)
        else _hide_from_users(group)
        for group in groups
    ]
    context["furo_navigation_tree"] = head + "".join(marked)


def setup(app):
    app.connect("html-page-context", _mark_developer_groups, priority=600)
