"""The sign-in page tells a new volunteer which address their invitation comes
from — this instance's sender, not another parish's — and offers the user
guide under a help icon to the reader who cannot get past it."""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.config import settings
from volunteerdb.ui.help_links import SIGNING_IN

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def test_the_login_page_names_this_instances_sender(database):
    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open("/login")
        await user.should_see(settings().mail_from)


def _help_hrefs(user) -> set[str]:
    """The targets under the help icon. The menu is built with the page, as
    the header's settings menu is, so the buttons are there to read before
    anybody opens it."""
    return {
        props.get("href")
        for button in user.find(kind=ui.button).elements
        if (props := button._props).get("href")
    }


async def test_the_sign_in_page_offers_the_guide_under_a_help_icon(database):
    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open("/login")
        await user.should_see("Help signing in")
        await user.should_see("Sign in with an emailed code")
        hrefs = _help_hrefs(user)
        for link in SIGNING_IN.links:
            assert link.href in hrefs, f"{link.title} is not offered at the door"
        assert all(
            "/manual/" in href for href in hrefs if href.startswith("/manual")
        ), "every help target is a page of the manual"


async def test_the_invite_page_offers_the_same_help(database):
    """A reader redeeming an invitation is on the page 'Sign in for the first
    time' was written for. A dead token is enough: the help is built with the
    page, not with the account."""
    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open("/invite/not-a-real-token")
        await user.should_see("Help signing in")
        assert SIGNING_IN.links[0].href in _help_hrefs(user)
