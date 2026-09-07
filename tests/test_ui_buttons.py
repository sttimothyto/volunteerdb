"""Clickable text is a button (uiux-improvement.md, step 37): a volunteer's
name, the photo, the site logo and the pending badge are real buttons with
accessible names, so the keyboard reaches them."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import events as event_service

from tests import mint
from tests.actors import as_volunteer
from tests.conftest import SIM_MAIN, SLOW, db_session, only, should_see_detail
from tests.fp_helpers import ok
from tests.test_ui_events import _parish, _seed_event


async def test_names_photos_and_the_logo_are_buttons(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])
    async with db_session() as session:
        view = ok(await event_service.detail(session, SYSTEM, event_id))
        ok(
            await event_service.sign_up(
                session,
                as_volunteer(ids["mia"]),
                slot_id=view.slots[0].slot.id,
                volunteer_id=ids["mia"],
                now=mint.now(),
            )
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        name = only(user.find("Mia Member", kind=ui.button))
        assert "vdb-namebtn" in name.classes
        assert name.props["aria-label"].startswith("Mia Member")
        user.find("Mia Member", kind=ui.button).click()
        await should_see_detail(user, "Email", "mia@example.org", retries=SLOW)

        # the photo on the profile page, for somebody who may change it
        await user.open(f"/volunteers/{ids['mia']}")
        photo = only(user.find(marker="photo-avatar"))
        assert isinstance(photo, ui.button)
        assert photo.props["aria-label"] == "Add or change photo"
        user.find(marker="photo-avatar").click()
        await user.should_see("Upload", retries=SLOW)

    async with db_session() as session:
        from volunteerdb.services import users

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
        logo = only(user.find(marker="site-logo"))
        assert isinstance(logo, ui.button)
        assert logo.props["aria-label"] == "Change the site logo"
        user.find(marker="site-logo").click()
        await user.should_see("Site logo", retries=SLOW)
