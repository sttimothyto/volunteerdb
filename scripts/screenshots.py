"""Every screen, as a picture: the evidence for a UI change.

Run: uv run python scripts/screenshots.py [--out screenshots] [--role admin|leader|member|anonymous]
                                          [--width 1280|390] [--light-only] [--only NAME] [--base-url URL]

Starts the app on a free port against the development database -- the
seeded parish, `make seed` -- with the scheduler off and NiceGUI's storage in
a temporary directory, the way tests/e2e/conftest.py starts it for the
browser tests. Then it drives Playwright's Chromium through the real sign-in
form as an administrator, a ministry leader, a plain member and an anonymous
reader, and writes

    screenshots/<role>/<page>@<width>[-dark].png

for every route docs/guide/reference/screens.md describes, at 1280 px and
390 px, light and dark, plus the dialogs a reader opens most (a new event,
editing a volunteer, signing up) and the side panel.

Same standing as scripts/bench.py: a local tool, not CI. `screenshots/` is
gitignored; the pictures are for eyeballing a change before and after, and
the plan that a change belongs to names the screens it moved. The ids the
detail pages need -- the biggest roster, an event the leader runs, an open
election -- are looked up in the database at the start, so a reseed never
breaks the script.

`--base-url` points it at a server already running (say `make dev`) and
skips the start-up; `--only` takes a substring of a page name.
"""

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from playwright.async_api import Browser, Page, async_playwright

from volunteerdb import db
from volunteerdb import env as env_mod
from volunteerdb.actors import load_actor
from volunteerdb.config import settings
from volunteerdb.db import transaction
from volunteerdb.models import ProposalStatus
from volunteerdb.services import elections as elections_service
from volunteerdb.services import events as event_service
from volunteerdb.services import reports as report_service
from volunteerdb.services import teams as team_service
from volunteerdb.services import users as user_service

# Every seeded account signs in with this (scripts/seed.py).
PASSWORD = "demo"
ROLES = {
    "admin": "admin@example.org",
    "leader": "maria.alvarez@example.org",
    "member": "felix.garcia@example.org",
}
WIDTHS = {1280: (1280, 900), 390: (390, 844)}
STARTUP_TIMEOUT = 60.0


@dataclass(frozen=True)
class Shot:
    """One picture: the page name in the file, the path to open, and -- for
    a dialog or the panel -- what to click once it is open."""

    name: str
    path: str
    click: str | None = None  # a button's accessible name
    click_marker: str | None = None  # or a NiceGUI marker on the page
    full_page: bool = True


async def resolve_ids() -> dict[str, int]:
    """The rows the detail pages are taken on, found rather than hard-coded:
    the biggest ordinary roster, the leader's own profile and an event she
    runs, one open and one decided election, an event the member can still
    sign up for."""
    env = env_mod.build(engine=db.make_engine(settings().database_url))
    ids: dict[str, int] = {}
    try:
        async with transaction(env, None) as session:
            now = env.clock.now()
            accounts = {
                role: await user_service.get_by_email(session, email)
                for role, email in ROLES.items()
            }
            missing = [r for r, a in accounts.items() if a is None]
            if missing:
                raise SystemExit(
                    f"no seeded account for {missing}: run `make seed` first"
                )
            leader = await load_actor(session, accounts["leader"])
            member = await load_actor(session, accounts["member"])
            ids["leader_volunteer"] = leader.volunteer_id or 0
            ids["member_volunteer"] = member.volunteer_id or 0

            tree = await team_service.tree(session)
            rows = await report_service.coverage(session)
            ordinary = [
                r
                for r in rows
                if "task force" not in tree.paths[r.team.id].lower()
                and r.team.is_active
            ]
            ids["big_team"] = max(ordinary, key=lambda r: r.total).team.id
            led = [t.id for t in tree.teams if leader.can_manage_team(t.id)]
            ids["leader_team"] = led[0] if led else ids["big_team"]

            upcoming = await event_service.list_events(session, leader, from_=now)
            managed = [s for s in upcoming if leader.can_manage_team(s.event.team_id)]
            ids["upcoming_event"] = (managed or upcoming)[0].event.id
            past = await event_service.list_events(session, leader, to=now)
            managed_past = [s for s in past if leader.can_manage_team(s.event.team_id)]
            ids["past_event"] = (managed_past or past)[-1].event.id
            joinable = await event_service.list_events(session, member, from_=now)
            open_to_member = [
                s
                for s in joinable
                if s.my_assignment is None
                and (s.capacity is None or s.filled < s.capacity)
            ]
            ids["member_event"] = (open_to_member or joinable or upcoming)[0].event.id

            # an election the leader runs, and a decided one anywhere (the
            # admin sees them all; the member and leader shots of it show
            # the refusal page, which is a screen of its own)
            admin = await load_actor(session, accounts["admin"])
            mine = await elections_service.list_proposals(
                session, leader, today=env.today()
            )
            every = await elections_service.list_proposals(
                session, admin, today=env.today()
            )
            open_ = [s for s in mine if s.proposal.status == ProposalStatus.open.value]
            decided = [
                s for s in every if s.proposal.status != ProposalStatus.open.value
            ]
            ids["open_proposal"] = (open_ or mine or every)[0].proposal.id
            ids["decided_proposal"] = (decided or every)[0].proposal.id
    finally:
        await env.engine.dispose()
    return ids


def shots_for(role: str, ids: dict[str, int]) -> list[Shot]:
    """The screens this role is taken through, in the order screens.md lists
    them. A page the role cannot open is left out rather than photographed
    as a refusal -- the refusal pages have a shot of their own below."""
    own = ids["leader_volunteer"] if role == "leader" else ids["member_volunteer"]
    if role == "anonymous":
        return [Shot("login", "/login"), Shot("ministries", "/ministries/")]
    shots = [
        Shot("dashboard", "/"),
        Shot("teams", "/teams"),
        Shot("team", f"/teams/{ids['big_team']}"),
        Shot("team-led", f"/teams/{ids['leader_team']}"),
        Shot("volunteers", "/volunteers"),
        Shot(
            "volunteer",
            f"/volunteers/{own if role != 'admin' else ids['leader_volunteer']}",
        ),
        Shot("events", "/events"),
        Shot("event", f"/events/{ids['upcoming_event']}"),
        Shot("event-past", f"/events/{ids['past_event']}"),
        Shot("elections", "/elections"),
        Shot("election", f"/elections/{ids['open_proposal']}"),
        Shot("election-decided", f"/elections/{ids['decided_proposal']}"),
        Shot("account", "/account"),
        Shot("panel", "/volunteers", click_marker="panel-open", full_page=False),
    ]
    if role == "admin":
        shots += [
            Shot("accounts", "/admin/users"),
            Shot("fields", "/admin/fields"),
            Shot("workload", "/admin/workload"),
            Shot("dialog-new-event", "/events", click="New event", full_page=False),
            Shot(
                "dialog-edit-volunteer",
                f"/volunteers/{ids['leader_volunteer']}",
                click="Edit",
                full_page=False,
            ),
        ]
    if role == "leader":
        shots += [
            Shot("dialog-new-event", "/events", click="New event", full_page=False),
            Shot(
                "dialog-edit-volunteer",
                f"/volunteers/{own}",
                click="Edit",
                full_page=False,
            ),
        ]
    if role == "member":
        shots += [
            Shot("denied-accounts", "/admin/users"),
            Shot(
                "dialog-sign-up",
                f"/events/{ids['member_event']}",
                click="Sign up",
                full_page=False,
            ),
        ]
    return shots


# --- the server ------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _drain(process: subprocess.Popen, output: list[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        output.append(line)


class LiveServer:
    """The app in its own process, the way `make serve` runs it, against the
    development database; nothing is stubbed."""

    def __init__(self, storage_dir: Path) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PYTEST_", "NICEGUI_"))
        }
        env |= {
            "VDB_HOST": "127.0.0.1",
            "VDB_PORT": str(self.port),
            "VDB_RELOAD": "false",
            "VDB_SCHEDULER_ENABLED": "false",
            "NICEGUI_STORAGE_PATH": str(storage_dir),
            "PYTHONUNBUFFERED": "1",
        }
        self.output: list[str] = []
        self.process = subprocess.Popen(
            [sys.executable, "-m", "volunteerdb.main"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(
            target=_drain, args=(self.process, self.output), daemon=True
        ).start()

    async def wait(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise SystemExit(
                        "the app exited before serving anything:\n"
                        + "".join(self.output[-40:])
                    )
                try:
                    response = await client.get(f"{self.base_url}/login")
                except httpx.HTTPError:
                    pass
                else:
                    if response.status_code == 200:
                        return
                await asyncio.sleep(0.1)
        self.stop()
        raise SystemExit(
            "the app did not serve /login in time:\n" + "".join(self.output[-40:])
        )

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
            self.process.kill()
            self.process.wait()


# --- the browser -----------------------------------------------------------------


async def ready(page: Page) -> None:
    """NiceGUI's websocket handshake: a click before it is a click the server
    never hears. The public pages have no socket, so a plain load will do."""
    # networkidle never comes on a NiceGUI page: the socket is open for good
    if await page.evaluate("typeof window.did_handshake !== 'undefined'"):
        await page.wait_for_function("window.did_handshake === true")
    else:
        await page.wait_for_load_state("load")


async def sign_in(page: Page, email: str) -> None:
    await page.goto("/login")
    await ready(page)
    await page.get_by_label("Email").fill(email)
    await page.get_by_label("Password (optional)").fill(PASSWORD)
    await page.get_by_role("button", name="Sign in", exact=True).click()
    await page.wait_for_url(lambda url: "/login" not in url)


async def sign_out(page: Page) -> None:
    await page.goto("/")
    await ready(page)
    await page.locator("button:has(i.q-icon:text-is('logout'))").click()
    await page.wait_for_url(lambda url: "/login" in url)


async def set_dark(page: Page) -> None:
    """The switch in the settings gear; the choice rides in this browser's
    session storage, so every later page of the same context is dark."""
    await page.goto("/")
    await ready(page)
    await page.locator("button:has(i.q-icon:text-is('settings'))").click()
    # a plain get_by_text("Dark mode") is ambiguous: a Guides link matches too
    await page.locator(".q-toggle", has_text="Dark mode").click()
    await page.wait_for_selector("body.body--dark")
    await page.keyboard.press("Escape")


async def take(page: Page, shot: Shot, target: Path) -> None:
    await page.goto(shot.path)
    await ready(page)
    if shot.click is not None:
        await page.get_by_role("button", name=shot.click, exact=True).first.click()
        await page.wait_for_selector(".q-dialog", state="visible")
        await page.wait_for_timeout(300)  # the dialog's enter transition
    elif shot.click_marker == "panel-open":
        # the first name in the volunteers table opens the side panel
        await page.locator("table tbody tr").first.click()
        await page.wait_for_selector(".q-drawer", state="visible")
        await page.wait_for_timeout(400)
    else:
        await page.wait_for_timeout(200)  # fonts and the last echart tick
    target.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(target), full_page=shot.full_page)


async def role_run(
    browser: Browser,
    base_url: str,
    role: str,
    shots: list[Shot],
    widths: list[int],
    out: Path,
    *,
    dark: bool,
    only: str,
) -> int:
    """One browser context per role: a fresh session, signed in once, every
    page at every width, then -- unless --light-only -- the switch flipped and
    the same pages again. The anonymous shots ride on the member's context:
    dark mode survives a sign-out on purpose (ui/context.py), so the
    signed-out pages can be photographed in it too."""
    taken = 0
    context = await browser.new_context(
        base_url=base_url, viewport={"width": 1280, "height": 900}
    )
    page = await context.new_page()
    wanted = [s for s in shots if only in s.name]
    try:
        if role != "anonymous":
            await sign_in(page, ROLES[role])
        for mode in ["light", "dark"] if dark else ["light"]:
            if mode == "dark":
                if role == "anonymous":
                    await sign_in(page, ROLES["member"])
                    await set_dark(page)
                    await sign_out(page)
                else:
                    await set_dark(page)
            for width in widths:
                w, h = WIDTHS[width]
                await page.set_viewport_size({"width": w, "height": h})
                for shot in wanted:
                    suffix = "-dark" if mode == "dark" else ""
                    target = out / role / f"{shot.name}@{width}{suffix}.png"
                    await take(page, shot, target)
                    taken += 1
                    print(f"  {target}")
    finally:
        await context.close()
    return taken


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="screenshots", help="directory to write into")
    parser.add_argument(
        "--role",
        choices=[*ROLES, "anonymous"],
        action="append",
        help="one role (repeatable); default: all four",
    )
    parser.add_argument(
        "--width", type=int, choices=list(WIDTHS), action="append", help="1280 or 390"
    )
    parser.add_argument("--light-only", action="store_true", help="skip dark mode")
    parser.add_argument("--only", default="", help="pages whose name contains this")
    parser.add_argument(
        "--base-url", default="", help="a server already running, e.g. make dev"
    )
    args = parser.parse_args(argv)

    roles = args.role or [*ROLES, "anonymous"]
    widths = args.width or list(WIDTHS)
    out = Path(args.out)

    print("resolving the seed's ids…")
    ids = await resolve_ids()
    print("  " + ", ".join(f"{k}={v}" for k, v in ids.items()))

    server: LiveServer | None = None
    base_url = args.base_url.rstrip("/")
    storage = tempfile.TemporaryDirectory(prefix="vdb-screenshots-")
    if not base_url:
        server = LiveServer(Path(storage.name))
        print(f"starting the app on {server.base_url}…")
        await server.wait()
        base_url = server.base_url

    taken = 0
    started = time.monotonic()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                for role in roles:
                    print(f"{role}:")
                    taken += await role_run(
                        browser,
                        base_url,
                        role,
                        shots_for(role, ids),
                        widths,
                        out,
                        dark=not args.light_only,
                        only=args.only,
                    )
            finally:
                await browser.close()
    finally:
        if server is not None:
            server.stop()
        storage.cleanup()
    print(f"{taken} screenshots in {out}/ ({time.monotonic() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
