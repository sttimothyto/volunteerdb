"""Headless UI test: email-code login flow, then the volunteer side panel.

Page routes register when the ui modules are imported, which must happen inside
the simulation's reset context (see ui_sim_main.py). On nicegui >= 3.14 that
works for multiple simulations per process: nicegui_reset_globals pops the page
modules from sys.modules after each one, so the next runpy of ui_sim_main.py
re-registers them. It skips modules named tests.* — tests/__init__.py must stay,
or the test modules themselves would be popped too.
"""

import re
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.ui.cytoscape_element import CytoscapeGraph

from .conftest import SLOW, mail_to
from tests import mint
from tests.conftest import db_session
from tests.fakes import SIM_MAILER
from tests.fp_helpers import ok

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_panel_opens_from_team_roster_table_and_graph(database, monkeypatch):
    SIM_MAILER.sent.clear()
    sent = SIM_MAILER.sent

    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        maria = ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "maria@example.org", "555-1234"
            )
        )
        ok(
            await memberships.assign(
                session, None, maria.id, liturgy.id, TeamRole.leader
            )
        )
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
        ok(
            await users.create(session, "felix@example.org", invite=mint.fresh_invite())
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
        _to, subject, _body = await mail_to(sent, "felix@example.org")
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
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        maria = ok(
            await volunteers.create(
                session, None, "Maria", "Alvarez", "maria@example.org"
            )
        )
        ok(
            await memberships.assign(
                session, None, maria.id, liturgy.id, TeamRole.member
            )
        )
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
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
