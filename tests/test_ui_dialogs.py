"""A dialog with fields stays until it is answered; a question does not.

forms.dialog_card marks every form dialog `persistent` -- Quasar then ignores
a click on the backdrop and the Escape key -- while forms.confirm leaves a
question dismissible, since a click beside it is the same answer as Cancel.
The simulation cannot click a backdrop, so this reads the prop the browser
would honour, on one dialog of each kind."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import teams, users

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok


async def test_a_form_dialog_is_persistent_and_a_question_is_not(database):
    async with db_session() as session:
        music = ok(await teams.create(session, SYSTEM, "Music"))
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        admin_id, music_id = admin.id, music.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/volunteers")
        user.find("New volunteer", kind=ui.button).click()
        await user.should_see("First name", retries=SLOW)
        form = only(user.find(kind=ui.dialog))
        assert "persistent" in form.props, "a form with fields must not vanish"
        # and Cancel is still on its row: the one deliberate way out
        user.find("Cancel", kind=ui.button).click()

        await user.open(f"/teams/{music_id}")
        user.find("Delete", kind=ui.button).click()
        await user.should_see("Delete the team Music?", retries=SLOW)
        question = only(user.find(kind=ui.dialog))
        assert "persistent" not in question.props, "a question is dismissible"
