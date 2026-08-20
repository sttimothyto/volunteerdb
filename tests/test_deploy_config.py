"""The deploy's site files, checked without deploying.

deploy/siteconf.py is stdlib-only precisely so this can import it: pyinfra is
not a dependency of the app, and a mistake in a site file should be caught by
`make test` rather than by a half-applied deploy.

The failure these guard against is quiet. A missing key leaves a template
variable unset, and Jinja renders an unset variable as the empty string — so a
typo in a site file becomes an `OnCalendar=` with no time in it, or a
`PublishPort=` with no port, installed and reloaded without complaint.
"""

import sys
from datetime import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
sys.path.insert(0, str(DEPLOY))

import siteconf  # noqa: E402  (needs the sys.path line above)

SITE_FILES = sorted(siteconf.SITES_DIR.glob("*.toml"))
SITE_NAMES = [p.stem for p in SITE_FILES]


def test_there_is_at_least_one_site_and_the_template():
    assert "example" in SITE_NAMES, (
        "deploy/sites/example.toml is what a new site copies"
    )
    assert len(SITE_NAMES) > 1, "no real site file"


@pytest.mark.parametrize("name", SITE_NAMES)
def test_site_file_loads_completely(name):
    """Every field populated, no unknown keys — both checked by load()."""
    site = siteconf.load(name)
    assert site.site_name == name


@pytest.mark.parametrize("name", SITE_NAMES)
def test_timezone_is_a_real_zone(name):
    from zoneinfo import ZoneInfo

    ZoneInfo(siteconf.load(name).site_timezone)


@pytest.mark.parametrize("name", SITE_NAMES)
def test_the_nightly_ordering_holds(name):
    """The one constraint between the five scheduled times, until now recorded
    only in prose in five places.

    The backup must precede the roster sync so its dump is a restore point
    taken immediately before the only automated bulk write in the system; the
    remaining in-app jobs come after both, so they never contend with either.
    """
    site = siteconf.load(name)
    ordered = [
        site.schedule_backup_at,
        site.schedule_roster_sync_at,
        site.schedule_fetch_pages_at,
        site.schedule_proposal_digest_at,
        site.schedule_event_reminders_at,
    ]
    parsed = [time.fromisoformat(v) for v in ordered]
    assert parsed == sorted(parsed), f"{name}: nightly times out of order: {ordered}"
    assert len(set(parsed)) == len(parsed), f"{name}: two jobs share a time: {ordered}"


def test_example_and_the_real_sites_have_identical_key_sets():
    """A template that has fallen behind is worse than none: it produces a site
    file that fails to load, or loads with a stale value nobody meant."""
    import tomllib

    def keys(path: Path) -> set[str]:
        return set(siteconf._flatten(tomllib.loads(path.read_text())))

    example = keys(siteconf.SITES_DIR / "example.toml")
    for path in SITE_FILES:
        assert keys(path) == example, f"{path.name} and example.toml disagree"


def test_a_missing_key_is_rejected(tmp_path, monkeypatch):
    """Rather than rendering an empty string into a systemd unit."""
    broken = (
        (siteconf.SITES_DIR / "example.toml")
        .read_text()
        .replace("listen_port = 8090", "")
    )
    (tmp_path / "broken.toml").write_text(broken)
    monkeypatch.setattr(siteconf, "SITES_DIR", tmp_path)
    with pytest.raises(SystemExit, match="missing host_listen_port"):
        siteconf.load("broken")


def test_a_typo_names_both_halves(tmp_path, monkeypatch):
    """A misspelled key is simultaneously an unknown key and a missing one.
    Reporting only the missing half leaves you hunting for a value that is
    right there in the file, spelled wrong."""
    broken = (
        (siteconf.SITES_DIR / "example.toml")
        .read_text()
        .replace("listen_port =", "lisen_port =")
    )
    (tmp_path / "broken.toml").write_text(broken)
    monkeypatch.setattr(siteconf, "SITES_DIR", tmp_path)
    with pytest.raises(SystemExit) as caught:
        siteconf.load("broken")
    message = str(caught.value)
    assert "missing host_listen_port" in message
    assert "unknown host_lisen_port" in message


def test_the_filename_must_match_the_declared_name(tmp_path, monkeypatch):
    """So that VDB_SITE=x always deploys x."""
    (tmp_path / "other.toml").write_text(
        (siteconf.SITES_DIR / "example.toml").read_text()
    )
    monkeypatch.setattr(siteconf, "SITES_DIR", tmp_path)
    with pytest.raises(SystemExit, match="but the file is other.toml"):
        siteconf.load("other")


def test_an_unset_site_names_the_choices(monkeypatch):
    monkeypatch.delenv("VDB_SITE", raising=False)
    with pytest.raises(SystemExit, match="VDB_SITE is not set"):
        siteconf.load()


def test_app_uid_matches_the_containerfile():
    """The drive-sync wrapper chowns its work dir to this uid so the container
    can write it; the Containerfile cannot be templated, since a plain
    `podman build .` has no site."""
    import re

    text = (REPO / "Containerfile").read_text()
    match = re.search(r"useradd .*--uid (\d+)", text)
    assert match, "Containerfile no longer creates the app user with an explicit uid"
    assert int(match.group(1)) == siteconf.APP_UID


def test_postgres_image_matches_compose():
    """Development and production run the same major version, or the schema
    that passes tests is not the schema production gets."""
    assert siteconf.PG_IMAGE in (REPO / "compose.yaml").read_text()


def _containerignore_entries() -> list[str]:
    text = (REPO / ".containerignore").read_text()
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_containerignore_and_the_sync_exclusions_account_for_each_other():
    """The two lists act on the same tree in sequence — files.sync ships it to
    the host, then podman builds from it applying .containerignore — so
    something excluded by neither ends up in the image.

    They are kept as two lists rather than derived one from the other because
    the glob semantics genuinely differ (files.sync's `exclude` is fnmatch
    over full REMOTE paths, hence the */ prefixes; `exclude_dir` affects
    traversal but not deletion) and because two entries are deliberately
    asymmetric. CONTAINERIGNORE_MAP records which is which.
    """
    entries = _containerignore_entries()
    mapped = siteconf.CONTAINERIGNORE_MAP

    unaccounted = [e for e in entries if e not in mapped]
    assert not unaccounted, (
        f".containerignore entries missing from CONTAINERIGNORE_MAP: {unaccounted}"
    )
    stale = [e for e in mapped if e not in entries]
    assert not stale, (
        f"CONTAINERIGNORE_MAP names entries not in .containerignore: {stale}"
    )

    known = set(siteconf.SYNC_EXCLUDE) | set(siteconf.SYNC_EXCLUDE_DIR)
    for entry, sync_rule in mapped.items():
        if sync_rule is None:
            continue  # deliberate: synced as build input, excluded from the image
        assert sync_rule in known, (
            f".containerignore {entry!r} maps to {sync_rule!r}, "
            "which is in neither SYNC_EXCLUDE nor SYNC_EXCLUDE_DIR"
        )


def test_the_build_inputs_are_synced_not_ignored():
    """The asymmetry the map allows, stated as an assertion: podman needs the
    Containerfile and .containerignore on the host to build from."""
    known = set(siteconf.SYNC_EXCLUDE) | set(siteconf.SYNC_EXCLUDE_DIR)
    for build_input in ("Containerfile", ".containerignore"):
        assert siteconf.CONTAINERIGNORE_MAP[build_input] is None
        assert build_input not in known, f"{build_input} must reach the host"


TEMPLATES = sorted((DEPLOY / "templates").glob("*.j2"))
# Rendered by loops that pass **unit_vars rather than naming each template.
LOOPED = {f"{n}.j2" for n in siteconf.QUADLETS + siteconf.TIMER_UNITS}
# deploy.py holds the running order; the operations live in steps/.
DEPLOY_SOURCES = [DEPLOY / "deploy.py", *sorted((DEPLOY / "steps").glob("*.py"))]


def _deploy_trees():
    import ast

    return [ast.parse(p.read_text()) for p in DEPLOY_SOURCES]


def _dict_keys(trees, node) -> set[str]:
    """Keys of a **kwargs expansion: a literal dict, a dict(...) call, or a
    name bound to either.

    The name is resolved across every deploy module, and case-insensitively,
    because UNIT_VARS is built in deploy.py and arrives in the steps as a
    parameter called unit_vars. Matching loosely is safe here: the risk this
    test exists to catch is a variable that is *not* supplied, and a name that
    resolves to the wrong dict would make the test stricter, not weaker.
    """
    import ast

    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant)}
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict":
        return {kw.arg for kw in node.keywords if kw.arg}
    if isinstance(node, ast.Name):
        for tree in trees:
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id.lower() == node.id.lower()
                    for t in stmt.targets
                ):
                    return _dict_keys(trees, stmt.value)
    return set()


def _template_calls():
    """Every files.template(...) call across the deploy, as
    (unparsed source, supplied keys)."""
    import ast

    trees = _deploy_trees()
    for tree in trees:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "template"
            ):
                supplied: set[str] = set()
                for keyword in node.keywords:
                    if keyword.arg is None:  # **unit_vars
                        supplied |= _dict_keys(trees, keyword.value)
                    else:
                        supplied.add(keyword.arg)
                yield ast.unparse(node), supplied


def test_every_template_is_rendered_somewhere():
    """A template nobody renders is dead weight that still looks maintained.

    The quadlets and timer units are named by siteconf rather than written
    out, so the check for those is that the lists and the files agree (below).
    """
    named = {src for src, _ in _template_calls()}
    for path in TEMPLATES:
        if path.name in LOOPED:
            continue
        assert any(path.name in src for src in named), f"{path.name} is never rendered"


def test_the_looped_templates_all_exist():
    """The other direction: siteconf lists a unit, but its template is gone."""
    present = {p.name for p in TEMPLATES}
    assert LOOPED <= present, f"missing templates: {sorted(LOOPED - present)}"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_variables_are_all_supplied(path):
    """The failure this exists for is silent.

    Jinja renders an undefined variable as the empty string, so a variable
    added to a template without a matching keyword at the call site becomes
    `PublishPort=127.0.0.1::8080`, or an `OnCalendar=` with no time in it —
    written, installed and daemon-reloaded without a word of complaint.

    Reading deploy.py with ast is cruder than importing it, but importing it
    needs pyinfra and a live host to gather facts from.
    """
    import jinja2
    import jinja2.meta

    needed = jinja2.meta.find_undeclared_variables(
        jinja2.Environment().parse(path.read_text())
    )

    calls = list(_template_calls())
    if path.name in LOOPED:
        # Every loop passes the same dict; any of those call sites answers.
        supplied = set().union(
            *(keys for src, keys in calls if "unit_vars" in src), set()
        )
    else:
        supplied = set().union(
            *(keys for src, keys in calls if path.name in src), set()
        )

    assert not (needed - supplied), (
        f"{path.name} uses {sorted(needed - supplied)}, which the deploy does "
        "not pass — they would render as empty strings"
    )
