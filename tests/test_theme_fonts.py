"""The faces the site serves (uiux-improvement.md, step 34): self-hosted,
Latin-subset, declared in theme.css and preloaded by main.py -- so the body
reads in the same serif on a phone, a Linux desktop and a Mac."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure

ROOT = Path(__file__).resolve().parents[1] / "src" / "volunteerdb"
FONTS = ROOT / "ui" / "static" / "fonts"
THEME = (ROOT / "ui" / "static" / "theme.css").read_text()
MAIN = (ROOT / "main.py").read_text()


def test_the_body_serif_is_self_hosted_and_small():
    faces = re.findall(r'src: url\("/static/fonts/([^"]+)"\)', THEME)
    assert "texgyrepagella-regular-latin.woff2" in faces
    assert "texgyrepagella-bold-latin.woff2" in faces
    for name in faces:
        path = FONTS / name
        assert path.exists(), f"{name} is declared but not served"
        assert path.stat().st_size < 80_000, f"{name} is not a Latin subset"
    assert re.search(r'--vdb-serif:\s*"TeX Gyre Pagella"', THEME), "the first choice"


def test_the_regular_faces_are_preloaded():
    """A face that is not preloaded arrives after the first paint and the
    text reflows; the two regulars are what the first paint needs."""
    assert "cinzel-v11-latin-regular" in MAIN
    assert "texgyrepagella-regular-latin" in MAIN
    assert "texgyrepagella-bold" not in MAIN, "bold is not on the first paint"
