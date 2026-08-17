"""Headless UI test: email-code login flow, then the volunteer side panel.

Page routes register when the ui modules are imported, which must happen inside
the simulation's reset context (see ui_sim_main.py). On nicegui >= 3.14 that
works for multiple simulations per process: nicegui_reset_globals pops the page
modules from sys.modules after each one, so the next runpy of ui_sim_main.py
re-registers them. It skips modules named tests.* — tests/__init__.py must stay,
or the test modules themselves would be popped too.
"""

import asyncio
import re
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import mail, memberships, teams, users, volunteers
from volunteerdb.ui.cytoscape_element import CytoscapeGraph

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

# should_see defaults to 3 retries x 0.1 s = a 300 ms budget. Every step of the
# email-code flow sits behind an argon2 pass — ~106 ms to hash the code, ~63 ms
# to verify it on an idle machine, and argon2 is configured for 64 MB and 4-way
# parallelism, so those multiply when the rest of the suite is competing for the
# same cores. 300 ms is close enough to the real cost that this test failed 2
# runs in 10 (measured), at whichever assertion happened to lose the race — so
# it failed on a different line each time, which is what made it look mysterious
# rather than slow. 3 s is nowhere near the real cost, and the extra budget is
# only ever spent on a genuine failure.
SLOW = 30


async def _mail_to(sent: list[tuple[str, str, str]], address: str, timeout_s=3.0):
    """The most recent email to `address`, waiting for it to arrive.

    The page reveals the code step after awaiting the send, so a visible hint
    ought to mean the mail is in — but the two are separated by an argon2 hash,
    and asserting on `sent` with no wait is the same race as a bare should_see.
    """
    for _ in range(int(timeout_s * 10)):
        if sent and sent[-1][0] == address:
            return sent[-1]
        await asyncio.sleep(0.1)
    raise AssertionError(f"no email to {address} within {timeout_s}s; sent={sent}")


async def test_panel_opens_from_team_roster_table_and_graph(database, monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> bool:
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(mail, "send_email", fake_send)

    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        maria = await volunteers.create(
            session, "Maria", "Alvarez", "maria@example.org", "555-1234"
        )
        await memberships.assign(session, maria.id, liturgy.id, TeamRole.leader)
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        await users.create(
            session, "felix@example.org"
        )  # passwordless -> email-code login
        team_id, maria_id, admin_id = liturgy.id, maria.id, admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        # unknown address: neutral code step is shown, but NO email is sent
        await user.open("/login")
        await user.should_see("Password (optional)")
        await user.should_see("Keep me signed in")
        user.find(kind=ui.input, content="Email").type("stranger@example.org")
        user.find(kind=ui.input, content="Password (optional)").trigger("keydown.enter")
        await user.should_see(
            "Enter the 6-digit code emailed to stranger@example.org", retries=SLOW
        )
        assert sent == []

        # OTP login: blank password emails a 6-digit code
        await user.open("/login")
        user.find(kind=ui.input, content="Email").type("felix@example.org")
        user.find(kind=ui.input, content="Password (optional)").trigger("keydown.enter")
        await user.should_see(
            "Enter the 6-digit code emailed to felix@example.org", retries=SLOW
        )
        _to, subject, _body = await _mail_to(sent, "felix@example.org")
        code = re.search(r"\b(\d{6})\b", subject).group(1)  # "…sign-in code: 123456"

        wrong = "000000" if code != "000000" else "111111"
        user.find(kind=ui.input, content="6-digit code").type(wrong)
        user.find(kind=ui.input, content="6-digit code").trigger("keydown.enter")
        await user.should_see("That code didn't work", retries=SLOW)

        user.find(kind=ui.input, content="6-digit code").clear()
        user.find(kind=ui.input, content="6-digit code").type(code)
        user.find(kind=ui.input, content="6-digit code").trigger("keydown.enter")
        await user.should_see("Volunteers", retries=SLOW)

        # switch to the admin for the panel checks
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        # team roster: clicking a member's name fills and opens the panel
        await user.open(f"/teams/{team_id}")
        user.find("Maria Alvarez", kind=ui.label).trigger("click")
        await user.should_see("Email: maria@example.org")
        await user.should_see("Phone: 555-1234")
        await user.should_see("Serves on")

        # volunteers list: a table rowClick does the same
        await user.open("/volunteers")
        user.find(kind=ui.table).trigger("rowClick", args=[None, {"id": maria_id}, 0])
        await user.should_see("Email: maria@example.org")

        # dashboard graph: clicking a volunteer node does the same
        await user.open("/")
        user.find(kind=CytoscapeGraph).trigger(
            "node_click", args={"type": "volunteer", "volunteer_id": maria_id}
        )
        await user.should_see("Email: maria@example.org")

        # volunteer detail: Gantt timeline replaced the as-of box
        await user.open(f"/volunteers/{maria_id}")
        await user.should_see("Service timeline")
        user.find(kind=ui.echart)
        await user.should_not_see("View as of (YYYY-MM-DD)")

        # the list page lost the box too; the teams page keeps it
        await user.open("/volunteers")
        await user.should_not_see("View as of (YYYY-MM-DD)")
        await user.open(f"/teams/{team_id}")
        await user.should_see("View as of (YYYY-MM-DD)")


async def test_photo_dialog_disclaimer_gates_upload(database):
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        maria = await volunteers.create(
            session, "Maria", "Alvarez", "maria@example.org"
        )
        await memberships.assign(session, maria.id, liturgy.id, TeamRole.member)
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        team_id, admin_id = liturgy.id, admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        await user.open(f"/teams/{team_id}")
        user.find("Maria Alvarez", kind=ui.label).trigger("click")
        await user.should_see("Serves on")

        # the person icon in the panel header opens the upload dialog
        user.find(marker="photo-avatar").trigger("click")
        await user.should_see("reported to the authorities")

        upload_button = user.find("Upload", kind=ui.button).elements.pop()
        assert not upload_button.enabled, (
            "Upload stays disabled until the declaration is checked"
        )
        checkbox = user.find(kind=ui.checkbox).elements.pop()
        checkbox.value = True
        assert upload_button.enabled, "checking the declaration enables Upload"
