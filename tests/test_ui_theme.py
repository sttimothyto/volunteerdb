"""Dark follows the device until the reader chooses (uiux-improvement.md,
step 31). Headless: the dark_mode element's value and the anti-flash style
the page carries; what the device says is a browser's to prove
(tests/e2e/test_browser_session.py)."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import users

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


async def test_no_choice_is_auto_and_a_choice_sticks(database):
    async with db_session() as session:
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        admin_id = admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/")
        assert only(user.find(kind=ui.dark_mode)).value is None, "auto: the device"
        # the anti-flash ground is on the device's say-so, like auto mode
        assert "prefers-color-scheme: dark" in user.client.head_html

        # the switch reads as off, and one click is the choice of dark
        switch = only(user.find("Dark mode", kind=ui.switch))
        assert not switch.value
        switch.value = True
        assert only(user.find(kind=ui.dark_mode)).value is True

        # kept for the next page, and from then on nothing follows the device
        await user.open("/volunteers")
        assert only(user.find(kind=ui.dark_mode)).value is True
        only(user.find("Dark mode", kind=ui.switch)).value = False
        await user.open("/")
        assert only(user.find(kind=ui.dark_mode)).value is False
