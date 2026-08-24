"""The sign-in page tells a new volunteer which address their invitation comes
from — this instance's sender, not another parish's."""

from pathlib import Path

from nicegui.testing.user_simulation import user_simulation

from volunteerdb.config import settings

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_the_login_page_names_this_instances_sender(database):
    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open("/login")
        await user.should_see(settings().mail_from)
