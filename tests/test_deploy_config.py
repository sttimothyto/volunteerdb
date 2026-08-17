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

    The backup must precede the Drive sync so its dump is a restore point
    taken immediately before the only automated bulk write in the system; the
    three in-app jobs must come after both, so they never contend with either.
    """
    site = siteconf.load(name)
    ordered = [
        site.schedule_backup_at,
        site.schedule_drive_sync_at,
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
