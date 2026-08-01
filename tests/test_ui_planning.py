"""Planning page: a leader proposes, an admin accepts, the roster updates."""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_propose_then_accept_flow(database):
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        lena = await volunteers.create(session, "Lena", "Leader")
        vera = await volunteers.create(session, "Vera", "Volunteer")
        await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
        leader = await users.create(
            session, "lena@example.org", volunteer_id=lena.id, password="pw"
        )
        admin = await users.create(session, "admin@example.org", is_admin=True, password="pw")
        leader_id, admin_id = leader.id, admin.id
        team_id, vera_id = liturgy.id, vera.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{leader_id}")
        await user.should_see("dev-login ok")

        await user.open("/planning")
        await user.should_see("Vacancies")
        await user.should_see("no second-in-command")

        # propose Vera for the vacant second-in-command slot (the form's default role)
        who = user.find(kind=ui.select, content="Volunteer").elements.pop()
        who.value = vera_id
        user.find("Propose", kind=ui.button).click()
        # "Vera Volunteer" alone would match the volunteer-select options even
        # before the post-propose reload lands; wait for the proposal row itself
        await user.should_see("proposed by lena@example.org", retries=30)

        # the admin decides
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/planning")
        await user.should_see("Vera Volunteer")
        user.find("Accept", kind=ui.button).click()
        await user.should_see("Every team has a leader and a second-in-command. 🎉", retries=30)
        await user.should_see("Accepted")

        await user.open(f"/teams/{team_id}")
        await user.should_see("Vera Volunteer")
