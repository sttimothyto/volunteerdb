"""Sphinx configuration for the VolunteerDB documentation.

Build:  uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
Links:  uv run --group docs sphinx-build -b linkcheck docs docs/_build/linkcheck
"""

import tomllib
from pathlib import Path

project = "VolunteerDB"
author = "St. Timothy Parish"
copyright = "2026 St. Timothy Parish"
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
    "deflist",      # definition lists for config vars and CLI synopses
    "tasklist",     # checklists in runbooks
]
myst_heading_anchors = 3

exclude_patterns = ["_build"]

html_theme = "furo"
html_title = "VolunteerDB"

# Strip shell prompts when copying code blocks.
copybutton_prompt_text = r"\$ |>>> "
copybutton_prompt_is_regexp = True

linkcheck_ignore = [
    r"http://localhost.*",
    r"http://127\.0\.0\.1.*",
    r"https://vdb\.sttimothyto\.org.*",  # auth-walled production instance
    r".*\.example.*",                    # seeded demo addresses
]
