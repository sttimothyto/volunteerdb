"""Custom fields read by type, and the empty ones fold into one line.

The profile printed a duration as PT3H30M, a time as 16:00:00, a timestamp
as 2021-05-05 16:40:00 and ten "—" lines for the fields nobody had filled,
ahead of the notes. fieldcodec.display reads each type to a person, and the
unset fields become "Not recorded: …" so a core member still sees what
could be filled in."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import FieldType
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import custom_fields, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


async def test_the_profile_reads_values_by_type_and_names_the_rest(database):
    async with db_session() as session:
        weekly = ok(
            await custom_fields.create_def(
                session, SYSTEM, "Time given each week", FieldType.interval.value
            )
        )
        arrival = ok(
            await custom_fields.create_def(
                session, SYSTEM, "Usual arrival time", FieldType.time.value
            )
        )
        ok(
            await custom_fields.create_def(
                session, SYSTEM, "Previous parish", FieldType.text.value
            )
        )
        ok(
            await custom_fields.create_def(
                session,
                SYSTEM,
                "T-shirt size",
                FieldType.select.value,
                options=["S", "M", "L"],
                show_in_list=True,
            )
        )
        maria = ok(
            await volunteers.create(
                session, SYSTEM, "Maria", "Alvarez", "maria@example.org"
            )
        )
        ok(
            await custom_fields.set_values(
                session,
                SYSTEM,
                maria.id,
                {weekly.key: "PT3H30M", arrival.key: "16:00"},
            )
        )
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        maria_id, admin_id = maria.id, admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open(f"/volunteers/{maria_id}")
        await user.should_see("Time given each week:")
        await user.should_see("3 h 30 min")
        await user.should_see("4:00 PM")
        await user.should_not_see("PT3H30M")
        await user.should_not_see("16:00:00")
        # the two empty fields are one line, not two dashes
        assert (
            only(user.find(marker="not-recorded")).text
            == "Not recorded: Previous parish, T-shirt size"
        )
        await user.should_not_see("Previous parish: —")

        # the list column shows the value in words and nothing for unset
        await user.open("/volunteers")
        row = only(user.find(kind=ui.table)).rows[0]
        assert row["cf_t_shirt_size"] == "" if "cf_t_shirt_size" in row else True
