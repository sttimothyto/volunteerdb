"""The header frame: one settings gear holds dark mode, the manual, and — only
on the pages that can time-travel — the as-of date picker. The address is the
way to your own record, and the headshot beside it is how you change it."""

from datetime import timedelta
from io import BytesIO
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation
from PIL import Image
from sqlalchemy.dialects.postgresql import insert as pg_insert

from volunteerdb.db import db_session
from volunteerdb.models import MailQuota
from volunteerdb.services import mail_quota, photos, teams, users, volunteers

from .conftest import SLOW
from tests.fp_helpers import ok

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

PICKER = "View as of (YYYY-MM-DD)"


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 120, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_settings_menu_carries_reading_preferences(database):
    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
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
        maria = ok(await volunteers.create(session, None, "Maria", "Alvarez"))
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
        felix = ok(await volunteers.create(session, None, "Felix", "Garcia"))
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
    photo could hang off, so nothing renders rather than something broken. The
    address beside it is plain text for the same reason — nowhere to send them."""
    async with db_session() as session:
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)
        admin_id = admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/volunteers")
        await user.should_see("Volunteers")
        await user.should_not_see(marker="header-avatar")
        # find() raises rather than returning empty, so name what it is
        address = user.find(marker="header-email").elements.pop()
        assert isinstance(address, ui.label), (
            "an unlinked account's address is a label, not a link"
        )


async def test_the_header_address_opens_your_own_record(database):
    """The most natural handle on "me" in the whole app was dead text."""
    async with db_session() as session:
        maria = ok(await volunteers.create(session, None, "Maria", "Alvarez"))
        account, _ = await users.create(
            session, "maria@example.org", volunteer_id=maria.id
        )
        maria_id, account_id = maria.id, account.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{account_id}")
        await user.open("/volunteers")
        link = user.find(marker="header-email", kind=ui.link).elements.pop()
        assert link.props["href"] == f"/volunteers/{maria_id}"

        await user.open(f"/volunteers/{maria_id}")
        await user.should_see("Maria Alvarez", retries=SLOW)


async def test_the_header_avatar_and_the_profile_avatar_are_addressed_apart(database):
    """Both are photo_avatar(); a test that means the one in the app bar must
    be able to say so, and test_volunteer_panel's marker must keep finding
    exactly the one on the page."""
    async with db_session() as session:
        maria = ok(await volunteers.create(session, None, "Maria", "Alvarez"))
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


# --- the mail-allowance banner ------------------------------------------------
#
# The mail provider allows 200 messages a day and 1,000 a month, and the app
# spends them without asking (nightly digests, sign-in codes). The banner is
# the one place it can warn somebody who can act — so the thing worth pinning
# is that "somebody" means an admin and nobody else.


async def _spend(day_offset: int, messages: int) -> None:
    day = mail_quota.local_today() - timedelta(days=day_offset)
    async with db_session() as session:
        await session.execute(
            pg_insert(MailQuota)
            .values(day=day, sent=messages)
            .on_conflict_do_update(index_elements=["day"], set_={"sent": messages})
        )


async def test_the_mail_allowance_banner_is_for_admins_only(database):
    """A volunteer can neither raise the plan nor stop the nightly digests, so
    a warning they cannot act on is noise on the page they came to read."""
    async with db_session() as session:
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)
        member, _ = await users.create(session, "member@example.org")
        admin_id, member_id = admin.id, member.id

    # One day past the 200/day cap, yesterday. Deliberately one and not a
    # week: seven of them would also blow the 1,000/month cap and read as
    # "critical", and this test is about who sees the banner, not how loud it
    # is. One day lands on "warning" wherever in the month it is run.
    await _spend(1, 250)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{member_id}")
        await user.open("/volunteers")
        await user.should_see("Volunteers")
        await user.should_not_see(marker="mail-quota-banner")

        await user.open(f"/login-dev/{admin_id}")
        await user.open("/volunteers")
        await user.should_see(marker="mail-quota-banner")
        await user.should_see("heading over its limit")
        await user.should_see("administrator who set up this website")


async def test_a_comfortable_instance_shows_no_banner_even_to_an_admin(database):
    async with db_session() as session:
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)
        admin_id = admin.id

    for day in range(1, 8):
        await _spend(day, 4)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/volunteers")
        await user.should_see("Volunteers")
        await user.should_not_see(marker="mail-quota-banner")


async def test_a_spent_day_reads_louder_than_one_merely_projected(database):
    async with db_session() as session:
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)
        admin_id = admin.id

    await _spend(0, mail_quota.DAILY_CAP)  # today's allowance already gone

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/volunteers")
        await user.should_see(marker="mail-quota-banner")
        await user.should_see("is over its limit")
        await user.should_see("including sign-in codes")
