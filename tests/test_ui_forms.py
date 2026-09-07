"""Required fields say so before the command runs.

forms.required marks a field ("Name *") and gives it a rule that refuses a
blank; forms.valid runs every field's rules when the button is clicked and
puts the focus on the first that fails. The message lives under the field,
where the reader is looking, rather than in a toast after the fact -- and
no command is run for a form that failed. The date and time inputs carry a
format rule of their own (ui/date_input.py)."""

from datetime import timedelta

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok


async def _parish(session) -> dict[str, int]:
    music = ok(await teams.create(session, SYSTEM, "Music"))
    lena = ok(
        await volunteers.create(session, SYSTEM, "Lena", "Leader", "lena@example.org")
    )
    ok(await memberships.assign(session, SYSTEM, lena.id, music.id, TeamRole.leader))
    admin, _ = ok(
        await users.create(
            session,
            "admin@example.org",
            is_admin=True,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    lena_u, _ = ok(
        await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    return {"music": music.id, "admin_u": admin.id, "lena_u": lena_u.id}


async def test_a_blank_required_field_says_so_and_nothing_is_saved(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/volunteers")
        user.find("New volunteer", kind=ui.button).click()
        await user.should_see("Last name", retries=SLOW)
        first = only(user.find(kind=ui.input, content="First name"))
        last = only(user.find(kind=ui.input, content="Last name"))
        assert first.props["label"] == "First name *", "the label carries the mark"

        user.find("Create", kind=ui.button).click()
        await user.should_see("Required", retries=SLOW)
        assert first.error == "Required" and last.error == "Required"
        await user.should_not_see("First and last name are required")

        first.value = "Vera"
        user.find("Create", kind=ui.button).click()
        await user.should_see("Required", retries=SLOW)
        assert first.error is None, "a filled field is clean again"
        assert last.error == "Required"
        # the dialog is still open: nothing ran
        await user.should_see("Last name")

        last.value = "Volunteer"
        user.find("Create", kind=ui.button).click()
        await user.should_see("Volunteer created", retries=SLOW)

    async with db_session() as session:
        assert await volunteers.name_map(session) == {2: "Vera Volunteer"} or any(
            name == "Vera Volunteer"
            for name in (await volunteers.name_map(session)).values()
        )


async def test_a_typed_date_that_is_not_one_is_refused_under_the_field(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/events")
        user.find("New event", kind=ui.button).click()
        await user.should_see("Repeat weekly until", retries=SLOW)
        only(user.find(kind=ui.select, content="Team")).value = ids["music"]
        only(user.find(kind=ui.input, content="Title")).value = "Bake sale"
        day = only(user.find(kind=ui.input, content="Date (YYYY-MM-DD)"))
        day.value = "next Sunday"
        user.find("Create event", kind=ui.button).click()
        await user.should_see("Use YYYY-MM-DD", retries=SLOW)
        assert day.error == "Use YYYY-MM-DD"
        await user.should_not_see("Start: use YYYY-MM-DD and HH:MM")

        day.value = str(mint.today() + timedelta(days=7))
        user.find("Create event", kind=ui.button).click()
        await user.should_see("Event created", retries=SLOW)


async def test_a_row_picker_left_empty_says_required_instead_of_toasting(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        who = only(user.find(kind=ui.select, content="Volunteer"))
        user.find(marker="add-member").click()
        await user.should_see("Required", retries=SLOW)
        assert who.error == "Required"
        await user.should_not_see("Pick a volunteer")
