"""The header frame: one settings gear holds dark mode, the manual, and — only
on the pages that can time-travel — the as-of date picker."""

from pathlib import Path

from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.services import teams, users

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

PICKER = "View as of (YYYY-MM-DD)"


async def test_settings_menu_carries_reading_preferences(database):
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        admin = await users.create(session, "admin@example.org", is_admin=True)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin.id}")

        # the two standalone header buttons are gone; their contents moved here
        await user.open("/volunteers")
        await user.should_see("Dark mode")
        await user.should_see("Manual")
        await user.should_not_see(PICKER)  # the volunteer list cannot time-travel

        # a page that reads history offers the picker in the same menu
        await user.open(f"/teams/{liturgy.id}")
        await user.should_see("Dark mode")
        await user.should_see(PICKER)
