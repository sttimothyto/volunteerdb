"""The header frame: one settings gear holds dark mode, the manual, and — only
on the pages that can time-travel — the as-of date picker. To the right of the
address it shows your own headshot, which is also how you change it."""

from io import BytesIO
from pathlib import Path

from nicegui.testing.user_simulation import user_simulation
from PIL import Image

from volunteerdb.db import db_session
from volunteerdb.services import photos, teams, users, volunteers

from .conftest import SLOW

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

PICKER = "View as of (YYYY-MM-DD)"


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 120, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_settings_menu_carries_reading_preferences(database):
    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin.id}")

        # the two standalone header buttons are gone; their contents moved here
        await user.open("/volunteers")
        await user.should_see("Dash")  # the brand, next to the nav links
        await user.should_see("Dark mode")
        await user.should_see("Manual")
        await user.should_not_see(PICKER)  # the volunteer list cannot time-travel

        # a page that reads history offers the picker in the same menu
        await user.open(f"/teams/{liturgy.id}")
        await user.should_see("Dark mode")
        await user.should_see(PICKER)


async def test_the_header_carries_the_signed_in_volunteers_headshot(database):
    async with db_session() as session:
        maria = await volunteers.create(session, "Maria", "Alvarez")
        await photos.set_photo(session, maria.id, _png(60, 60), uploaded_by=None)
        account, _ = await users.create(
            session, "maria@example.org", volunteer_id=maria.id
        )
        maria_id, account_id = maria.id, account.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{account_id}")
        await user.open("/volunteers")
        avatar = user.find(marker="header-avatar").elements.pop()
        assert avatar.source.startswith(f"/photos/{maria_id}?v="), (
            "the header shows their own headshot, cache-busted by upload time"
        )
        # the same dialog the profile page opens: one upload workflow, one
        # legal declaration
        user.find(marker="header-avatar").trigger("click")
        await user.should_see("reported to the authorities", retries=SLOW)


async def test_the_header_offers_an_upload_when_there_is_no_photo_yet(database):
    async with db_session() as session:
        felix = await volunteers.create(session, "Felix", "Garcia")
        account, _ = await users.create(
            session, "felix@example.org", volunteer_id=felix.id
        )
        account_id = account.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{account_id}")
        await user.open("/volunteers")
        # the person icon stands in, and is still the way to add one
        assert user.find(marker="header-avatar").elements, (
            "a volunteer with no photo still gets the placeholder to click"
        )


async def test_an_account_with_no_volunteer_record_gets_no_header_avatar(database):
    """The sync bot, and any admin nobody linked: there is no volunteer row a
    photo could hang off, so nothing renders rather than something broken."""
    async with db_session() as session:
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)
        admin_id = admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/volunteers")
        await user.should_see("Volunteers")
        await user.should_not_see(marker="header-avatar")


async def test_the_header_avatar_and_the_profile_avatar_are_addressed_apart(database):
    """Both are photo_avatar(); a test that means the one in the app bar must
    be able to say so, and test_volunteer_panel's marker must keep finding
    exactly the one on the page."""
    async with db_session() as session:
        maria = await volunteers.create(session, "Maria", "Alvarez")
        account, _ = await users.create(
            session, "maria@example.org", volunteer_id=maria.id
        )
        maria_id, account_id = maria.id, account.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{account_id}")
        await user.open(f"/volunteers/{maria_id}")
        await user.should_see("Maria Alvarez")
        assert len(user.find(marker="photo-avatar").elements) == 1
        assert len(user.find(marker="header-avatar").elements) == 1
