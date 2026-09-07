"""Team weights are a table.

One number per team was a row of widgets per team with no search and a
label a phone could not show. The table's rows carry the weights; the
Weight cell's input emits what is typed with its row, the handler writes it
into the rows, and Save weights diffs the rows against what the page
loaded -- the widget is the state, as everywhere else in ui/."""

from decimal import Decimal

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import teams, users

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok


async def test_weights_are_grouped_searchable_and_saved_from_the_rows(database):
    async with db_session() as session:
        liturgy = ok(await teams.create(session, SYSTEM, "Liturgy"))
        choir = ok(
            await teams.create(session, SYSTEM, "Choir", parent_team_id=liturgy.id)
        )
        ok(await teams.create(session, SYSTEM, "Hospitality"))
        ok(await teams.update(session, SYSTEM, choir.id, workload_weight=Decimal("1")))
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        choir_id, admin_id = choir.id, admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.open("/admin/workload")
        await user.should_see("Saving recolours the badges")
        table = only(user.find(marker="weights"))
        assert [c["name"] for c in table.columns] == ["ministry", "team", "weight"]
        by_path = {r["path"]: r for r in table.rows}
        assert by_path["Liturgy / Choir"]["ministry"] == "Liturgy", (
            "grouped by ministry"
        )
        assert by_path["Liturgy / Choir"]["weight"] == 1.0
        await user.should_see("3 teams")

        user.find(marker="weights-search").type("choir")
        assert [r["path"] for r in only(user.find(marker="weights")).rows] == [
            "Liturgy / Choir"
        ]
        await user.should_see("1 of 3 teams")

        # the typed weight lands in the row; Save diffs the rows
        user.find(marker="weights").trigger("weight", {"id": choir_id, "value": "2.5"})
        user.find("Save weights", kind=ui.button).click()
        await user.should_see("Updated 1 team weight", retries=SLOW)

    async with db_session() as session:
        choir = await teams.get(session, choir_id)
        assert choir is not None and choir.workload_weight == Decimal("2.5")
