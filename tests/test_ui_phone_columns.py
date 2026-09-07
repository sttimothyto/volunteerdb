"""Tables on the phone show what a phone can show.

At 390 px the events table showed two of its six columns and the volunteers
table two of nine, the rest scrolling sideways inside the table. Each table
now marks the columns a phone cannot show (theme.css hides .vdb-col-wide
below 40rem and .vdb-col-md below a tablet's width) and grows a second line
in its first cell for what they held. The marks ride on the column dicts,
so column_order's permutation keeps them; the cascade itself is a browser
test (tests/e2e/test_browser_layout.py)."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import FieldType, TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import custom_fields, memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


def _classes(table: ui.table) -> dict[str, str]:
    return {c["name"]: c.get("classes", "") for c in table.columns}


async def test_each_listing_marks_what_a_phone_hides(database):
    async with db_session() as session:
        music = ok(await teams.create(session, SYSTEM, "Music"))
        lena = ok(
            await volunteers.create(
                session, SYSTEM, "Lena", "Leader", "lena@example.org", "555-0100"
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, lena.id, music.id, TeamRole.leader
            )
        )
        ok(
            await custom_fields.create_def(
                session, SYSTEM, "Diocese", FieldType.text.value, show_in_list=True
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
        admin_id = admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")

        await user.open("/events")
        events = _classes(only(user.find(kind=ui.table)))
        assert {k for k, v in events.items() if v == "vdb-col-wide"} == {
            "team",
            "location",
            "you",
        }
        assert events["when"] == "vdb-col-tight", "a date never wraps"
        assert events["title"] == "" and events["filled"] == ""

        await user.open("/volunteers")
        vols = _classes(only(user.find(kind=ui.table)))
        assert vols["email"] == "vdb-col-wide" and vols["phone"] == "vdb-col-wide"
        assert vols["cf_diocese"] == "vdb-col-md", "a custom column is a desktop's"
        assert vols["name"] == "" and vols["workload"] == "" and vols["status"] == ""

        await user.open("/teams")
        teams_ = _classes(only(user.find(kind=ui.table)))
        assert {k for k, v in teams_.items() if v == "vdb-col-wide"} == {
            "leader",
            "second",
            "core",
            "member",
            "total",
        }
        assert teams_["gaps"] == "", "the column a leader acts on stays"

        await user.open("/admin/users")
        accounts = _classes(only(user.find(marker="accounts")))
        assert accounts["status"] == "vdb-col-wide", "the badge moves under the address"
        assert accounts["last_login"] == "vdb-col-wide"
        assert accounts["email"] == "" and accounts["actions"] == ""
