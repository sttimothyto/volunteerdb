"""The configuration surface, kept honest mechanically.

Three files describe the same settings for three audiences: config.py defines
them, .env.example is what you copy, and docs/reference/configuration.md is
what you look them up in. Nothing bound the three together, and they drifted —
.env.example claimed an invite link lasted 24 hours when the real default was
168, and omitted six settings entirely.

Prose is deliberately not compared here: the comments in config.py are
implementer rationale and the manual is operator guidance, and flattening one
into the other would lose both. What these tests pin is the mechanical part —
that every setting is mentioned everywhere it should be, that nothing is
mentioned that no longer exists, and that stated defaults are the real ones.

The convention .env.example follows, stated at the top of that file: a
commented assignment carries the setting's real default, and illustrative
non-default values live in the prose above it.
"""

import re
from pathlib import Path

import pytest

from volunteerdb.config import LOG_LEVELS, Settings
from volunteerdb.log import _MODE_NUM

pytestmark = pytest.mark.pure

REPO = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO / ".env.example"
CONFIG_DOC = REPO / "docs" / "reference" / "configuration.md"

# VDB_* variables that are deliberately NOT Settings fields. Each is read by
# something other than the running app, so a reader meeting one in .env.example
# or the manual should still find it accounted for here.
NON_SETTINGS_VARS = {
    "VDB_ADMIN_EMAIL",  # volunteerdb.admin_bootstrap
    "VDB_ADMIN_PASSWORD",  # volunteerdb.admin_bootstrap
    "VDB_SEED_ADMIN_PASSWORD",  # scripts/seed.py
    "VDB_CONTACT_EMAIL",  # docs/conf.py, at docs-build time
    "VDB_DB_PASSWORD",  # written by the deploy, read back by the deploy
    "VDB_TEST_ALLOW_NO_DB",  # tests/conftest.py
}

_ASSIGNMENT = re.compile(r"^#?\s*(VDB_[A-Z0-9_]+)=(.*)$", re.MULTILINE)
_MENTION = re.compile(r"\bVDB_[A-Z0-9_]+\b")
# A definition-list term in configuration.md: a line that is nothing but
# backticked variable names, e.g. `VDB_HOST` or the grouped
# `VDB_FETCH_PAGES_AT`, `VDB_PROPOSAL_DIGEST_AT`, `VDB_EVENT_REMINDERS_AT`.
_TERM_LINE = re.compile(r"^`VDB_[A-Z0-9_]+`(?:,\s*`VDB_[A-Z0-9_]+`)*$", re.MULTILINE)


def _assigned(env_example: str) -> set[str]:
    """Variables .env.example actually assigns, commented or not — as opposed
    to ones it merely names in a prose comment."""
    return {var for var, _ in _ASSIGNMENT.findall(env_example)}


def _defined(config_doc: str) -> set[str]:
    """Variables configuration.md gives their own definition-list entry."""
    return {
        v for line in _TERM_LINE.findall(config_doc) for v in _MENTION.findall(line)
    }


def _fields() -> dict[str, str]:
    """Settings field name -> the VDB_ variable that sets it."""
    return {name: f"VDB_{name.upper()}" for name in Settings.model_fields}


@pytest.fixture(scope="module")
def env_example() -> str:
    return ENV_EXAMPLE.read_text()


@pytest.fixture(scope="module")
def config_doc() -> str:
    return CONFIG_DOC.read_text()


def test_every_setting_appears_in_env_example(env_example):
    """A new field that nobody can discover is a field nobody will set.

    Requires a real assignment line, not just a mention: several settings are
    named in passing inside other settings' comments, and counting those would
    let a genuinely missing one pass.
    """
    missing = sorted(set(_fields().values()) - _assigned(env_example))
    assert not missing, f".env.example does not assign: {', '.join(missing)}"


def test_every_setting_appears_in_the_configuration_reference(config_doc):
    """Likewise: its own definition-list entry, not a cross-reference from
    somewhere else's prose."""
    missing = sorted(set(_fields().values()) - _defined(config_doc))
    assert not missing, f"configuration.md does not document: {', '.join(missing)}"


def test_no_stale_variables_are_documented(env_example, config_doc):
    """The other direction: a renamed or deleted setting left behind in either
    file reads as current, which is worse than an omission."""
    known = set(_fields().values()) | NON_SETTINGS_VARS
    for label, text in (
        (".env.example", env_example),
        ("configuration.md", config_doc),
    ):
        stale = sorted(set(_MENTION.findall(text)) - known)
        assert not stale, f"{label} mentions unknown variables: {', '.join(stale)}"


def test_env_example_states_the_real_defaults(env_example):
    """The assertion that would have caught the 24-vs-168 invite TTL.

    Compares against model_fields[...].default rather than Settings(), which
    would read the developer's own .env and quietly agree with itself.
    """
    by_var = {var: name for name, var in _fields().items()}
    mismatched = []
    for var, raw in _ASSIGNMENT.findall(env_example):
        name = by_var.get(var)
        if name is None:  # a NON_SETTINGS_VARS entry; nothing to compare
            continue
        default = Settings.model_fields[name].default
        # Round-trip through the model so "false"/"03:00" compare as the
        # bool/time they parse to, not as strings.
        parsed = getattr(Settings(**{name: raw.strip()}), name)
        if parsed != default:
            mismatched.append(f"{var}={raw.strip()!r} but the default is {default!r}")
    assert not mismatched, "\n".join(mismatched)


def test_healthchecks_port_fallback_matches_the_settings_default():
    """healthcheck.py stays free of volunteerdb imports on purpose — a probe
    that fails because settings will not parse reports nothing useful, and it
    runs every 30 seconds. The price is a duplicated default, pinned here."""
    text = (REPO / "healthcheck.py").read_text()
    match = re.search(r'os\.environ\.get\("VDB_PORT",\s*"(\d+)"\)', text)
    assert match, "healthcheck.py no longer reads VDB_PORT with a literal fallback"
    assert int(match.group(1)) == Settings.model_fields["port"].default


def test_log_level_names_match_the_handler_thresholds():
    """config.py validates the name, log.py maps it to a number. If the two
    sets diverge, a value config accepts raises KeyError inside init_logging."""
    assert set(LOG_LEVELS) == set(_MODE_NUM)


def test_the_database_url_default_is_mirrored_where_it_has_to_be():
    """The dev database URL is spelled out in several files that cannot import
    config.py — .env.example, the CI workflow, compose.yaml, the Makefile.
    They are allowed to be literals; they are not allowed to disagree."""
    default = Settings.model_fields["database_url"].default
    for relative in (".env.example", ".github/workflows/ci.yml"):
        assert default in (REPO / relative).read_text(), (
            f"{relative} no longer carries the VDB_DATABASE_URL default"
        )

    # compose.yaml points at the `db` service rather than localhost, so only
    # the credentials and database name are shared with the default.
    user = default.split("://", 1)[1].split(":", 1)[0]
    dbname = default.rsplit("/", 1)[1]
    compose = (REPO / "compose.yaml").read_text()
    makefile = (REPO / "Makefile").read_text()
    assert f"POSTGRES_USER: {user}" in compose
    assert f"POSTGRES_DB: {dbname}" in compose
    assert f"DB_USER := {user}" in makefile
