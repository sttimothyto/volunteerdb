"""Browser tests: the seams no headless harness can reach.

The rest of `tests/` proves this app without a browser — services against the
database, the JSON API over ASGI, the pages through NiceGUI's user simulation.
A few things stay out of reach that way, and the modules that own them say so:

* a real drag — "That leaves exactly one untested link (mousedown-move-drop ->
  $emit), which is the part to check by hand in a browser"
  (tests/test_ui_column_order.py);
* the CSS cascade — "Nothing in a headless render catches this ... only the
  browser cascade hides it. So this reads the source"
  (tests/test_ui_css_invariants.py);
* ui/cytoscape_graph.js and ui/static/column_drag.js, which are never executed
  at all without a DOM.

So a test belongs in this package only when a browser is what makes it
possible; anything provable headlessly is faster and steadier in the suite next
door, and these tests should stay few enough to run on every `make test`.

The fixtures below start the *real* app — `python -m volunteerdb.main`, the
entry point `make serve` uses — in its own process against the same scratch
`volunteerdb_test` database, and drive it with Playwright's Chromium. Nothing
is stubbed and no dev shortcut is wired in: signing in means filling the login
form, and every interaction goes over the websocket the app really uses.
"""

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Locator, Page, Playwright, expect

from ..conftest import TEST_URL

STARTUP_TIMEOUT = 60.0  # a cold `python -m volunteerdb.main` plus uvicorn bind
SHUTDOWN_GRACE = 10.0
# Fixed so a session cookie stays readable across a server restart within one
# run; it protects nothing but a scratch database on the loopback interface.
STORAGE_SECRET = "e2e-browser-tests-storage-secret"


def _free_port() -> int:
    """A port nothing is listening on, released again immediately.

    Racy in principle — something could claim it in the microseconds before
    uvicorn binds — and fine in practice on a developer's box or a runner. A
    fixed port would be the worse trade: it collides with the `make dev` server
    that is very often already running while these tests are written.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass
class LiveServer:
    """The app under test, and everything it has said on its way there."""

    base_url: str
    process: subprocess.Popen
    output: list[str] = field(default_factory=list)

    def tail(self, lines: int = 40) -> str:
        """The end of the server's log, for a failure message worth reading."""
        return "".join(self.output[-lines:]) or "(the server said nothing)"


def _drain(process: subprocess.Popen, output: list[str]) -> None:
    """Keep the server's pipe empty.

    Not optional: a server whose stdout buffer fills up blocks on its next log
    line, which under VDB_LOG_LEVEL=INFO means it stops serving mid-test and
    the failure looks like a timeout with no cause.
    """
    assert process.stdout is not None
    for line in process.stdout:
        output.append(line)


@pytest.fixture(scope="session")
async def live_server(database, tmp_path_factory) -> AsyncIterator[LiveServer]:
    """One real server for the whole browser session.

    Per-test isolation comes from the two fixtures either side of it: the
    autouse `clean_tables` truncates the database between tests, and Playwright
    hands every test a fresh browser context (so a fresh session cookie, and a
    fresh session id in the server's storage). What the process itself carries
    across tests is only what a restart would clear anyway.
    """
    port = _free_port()
    # PYTEST_* and NICEGUI_* are stripped rather than inherited. NiceGUI decides
    # it is "running in pytest" from PYTEST_CURRENT_TEST alone (helpers.is_pytest),
    # and a server that believes that reads its port from NICEGUI_SCREEN_TEST_PORT
    # — a variable only NiceGUI's own selenium plugin sets — and dies on the
    # KeyError. NICEGUI_USER_SIMULATION, set by the headless UI tests that ran
    # before this one, would misroute the app just as thoroughly.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "NICEGUI_"))
    }
    env |= {
        "VDB_DATABASE_URL": TEST_URL,
        "VDB_HOST": "127.0.0.1",
        "VDB_PORT": str(port),
        "VDB_RELOAD": "false",  # a reload worker would orphan itself on teardown
        # Nightly jobs firing against the scratch database — mailing digests,
        # fetching team home pages — are nobody's idea of a test fixture.
        "VDB_SCHEDULER_ENABLED": "false",
        "VDB_STORAGE_SECRET": STORAGE_SECRET,
        # NiceGUI's per-session files. Left at its default this would write
        # into the repo's own .nicegui/, which belongs to `make dev`.
        "NICEGUI_STORAGE_PATH": str(tmp_path_factory.mktemp("nicegui")),
        "PYTHONUNBUFFERED": "1",  # or `tail()` shows nothing when it matters
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "volunteerdb.main"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    server = LiveServer(f"http://127.0.0.1:{port}", process)
    threading.Thread(target=_drain, args=(process, server.output), daemon=True).start()
    await _wait_until_serving(server)
    try:
        yield server
    finally:
        process.terminate()
        try:
            process.wait(timeout=SHUTDOWN_GRACE)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
            process.kill()
            process.wait()


async def _wait_until_serving(server: LiveServer) -> None:
    """Block until /login answers, or fail with the server's own log.

    /login rather than /: it is the one page that renders for an anonymous
    browser, so a 200 here means the whole stack — uvicorn, the middleware, the
    page routes — is actually up, not merely bound.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            if server.process.poll() is not None:
                pytest.fail(
                    f"the app exited with {server.process.returncode} before it "
                    f"served anything:\n{server.tail()}",
                    pytrace=False,
                )
            try:
                response = await client.get(f"{server.base_url}/login")
            except httpx.HTTPError:
                pass
            else:
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.1)
    server.process.kill()
    pytest.fail(
        f"the app did not serve /login within {STARTUP_TIMEOUT:.0f}s:\n{server.tail()}",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def base_url(live_server) -> str:
    """Where `page.goto("/volunteers")` goes.

    Overrides pytest-base-url's fixture, which pytest-playwright folds into
    every browser context — so this is also what makes the server start before
    the first context is created.
    """
    return live_server.base_url


@pytest.fixture(scope="session", autouse=True)
async def chromium_installed(playwright: Playwright) -> None:
    """Fail, don't skip, when the browser was never downloaded.

    Same argument tests/conftest.py makes about Postgres: a skip exits 0, so a
    run that never opened a browser would report the same green as one that
    did. Set VDB_TEST_ALLOW_NO_BROWSER=1 to opt into skipping deliberately —
    the counterpart of VDB_TEST_ALLOW_NO_DB.
    """
    if Path(playwright.chromium.executable_path).exists():
        return
    message = (
        "Playwright's Chromium is not installed.\n"
        "Install it with:  uv run playwright install --with-deps chromium\n"
        "To skip the browser tests deliberately, set VDB_TEST_ALLOW_NO_BROWSER=1."
    )
    if os.environ.get("VDB_TEST_ALLOW_NO_BROWSER") == "1":
        pytest.skip(message)
    pytest.fail(message, pytrace=False)


@pytest.fixture(scope="session", autouse=True)
def unhurried_assertions() -> None:
    """Give `expect()` longer than its 5 s default to come true.

    The same stopwatch argument tests/conftest.py makes for SLOW: a sign-in is
    an argon2 verify, and a page here is a real HTTP round trip plus a socket
    handshake plus whatever the rest of the suite is doing to the same cores at
    that moment. The extra seconds are only ever spent on a genuine failure.
    """
    expect.set_options(timeout=15_000)


@pytest.fixture
async def page(page: Page) -> AsyncIterator[Page]:
    """Playwright's page, with uncaught JavaScript errors made fatal.

    This app hand-writes three browser-side files (cytoscape_graph.js,
    column_drag.js, the Vue slots in the page modules), and a throw in any of
    them leaves the server-rendered page looking perfectly correct — the very
    failure this package exists to catch. NiceGUI's own Screen fixture takes
    the same position (Screen.CATCH_JS_ERRORS).
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"{exc}"))
    yield page
    assert not errors, "the browser reported uncaught JavaScript errors:\n" + "\n".join(
        errors
    )


def icon_button(page: Page, icon: str) -> Locator:
    """The header's icon-only buttons — settings, sign out, the narrow-window
    menu — located by the icon they carry.

    They have no accessible name to ask for: Quasar renders a material icon as
    an <i> whose text is the ligature ("logout") and marks it aria-hidden, and
    the NiceGUI .tooltip() beside it is a QTooltip, not an aria-label. So the
    ligature text is the only handle, and a screen reader announces these
    buttons as "button" — worth fixing in the app, not worth faking here.
    """
    return page.locator(f"button:has(i.q-icon:text-is('{icon}'))")


async def ready(page: Page) -> None:
    """Wait for the websocket handshake NiceGUI does after the page loads.

    Every button on a NiceGUI page is a socket round-trip, so a click landing
    before this is a click the server may never hear about. `did_handshake` is
    the flag NiceGUI's own client sets when the connection is usable.
    """
    await page.wait_for_function("window.did_handshake === true")


async def sign_in(page: Page, email: str, password: str) -> None:
    """Sign in the way a person does: the real form, over the real socket.

    Deliberately not a cookie or a storage write — those would skip exactly the
    path this suite is here to exercise, and the app's own sign-in happens over
    the websocket where no Set-Cookie can be sent (see main.AuthMiddleware).
    """
    await page.goto("/login")
    await ready(page)
    await page.get_by_label("Email").fill(email)
    await page.get_by_label("Password (optional)").fill(password)
    await page.get_by_role("button", name="Sign in", exact=True).click()
    # Waiting for the navigation, not just the click: the click only puts a
    # message on the socket, and a caller that went straight on to page.goto()
    # would race the session into existence and land back on /login.
    await page.wait_for_url(lambda url: "/login" not in url)
