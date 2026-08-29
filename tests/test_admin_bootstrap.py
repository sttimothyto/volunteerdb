"""The admin bootstrap — the only way into a brand-new instance.

These tests became possible when the script moved out of `deploy/files/` and
into the package. That move is the point: `.containerignore` excludes
`deploy/`, so an image built by a plain `podman build .` never contained the
old file, and nothing here could import it.

The behavior worth pinning is the ordering. A rejected `VDB_ADMIN_PASSWORD`
must fail with one readable line *before* the database is touched, because
this runs as a one-shot container mid-deploy: a traceback out of the password
layer and a half-open connection are both worse than a clean exit 2.
"""

import pytest

from volunteerdb import admin_bootstrap
from volunteerdb.services import users

from tests.conftest import db_session


@pytest.fixture
def admin_env(monkeypatch):
    """Set the two variables the bootstrap requires; return a setter for the
    password so each test can choose one."""

    def _set(password: str, email: str = "admin@example.org") -> None:
        monkeypatch.setenv("VDB_ADMIN_EMAIL", email)
        monkeypatch.setenv("VDB_ADMIN_PASSWORD", password)

    return _set


async def test_weak_password_exits_2_without_touching_the_database(
    admin_env, monkeypatch, capsys
):
    """The whole reason the policy check precedes the transaction. If this
    inverts, a bad deploy password produces a traceback instead of a reason."""
    admin_env("demo")  # four characters — far below the 15-char minimum

    def _explode():  # pragma: no cover - must never be reached
        raise AssertionError("database opened before the password was checked")

    monkeypatch.setattr(admin_bootstrap, "transaction", _explode)

    assert await admin_bootstrap.main() == 2
    assert "VDB_ADMIN_PASSWORD rejected" in capsys.readouterr().err


async def test_creates_the_admin_then_is_a_no_op(database, admin_env, capsys):
    """Idempotent by design: the deploy calls this on every run that passes a
    password, including runs where the account has existed for months."""
    admin_env("otter lamp fig quilt")

    assert await admin_bootstrap.main() == 0
    assert "created admin admin@example.org" in capsys.readouterr().out

    assert await admin_bootstrap.main() == 0
    assert "already exists" in capsys.readouterr().out


async def test_the_created_account_is_an_administrator(database, admin_env):
    """A bootstrap that produced a non-admin would lock the instance out just
    as thoroughly as producing nothing."""
    admin_env("otter lamp fig quilt")
    await admin_bootstrap.main()

    async with db_session() as session:
        user = await users.get_by_email(session, "admin@example.org")
        assert user is not None and user.is_admin
