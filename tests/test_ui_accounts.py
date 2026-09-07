"""Accounts is a table.

Thirty-three accounts were 2,665 px of rows, each with four icon-only
buttons whose meaning was in a tooltip; on a phone the icons wrapped under
the address. The table is searched, sorted and paged, and the four actions
sit in a "⋯" menu per row, named in words. The menu's items emit events with
their row, which the tests fire."""

from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok


async def _parish(session) -> dict[str, int]:
    admin, _ = ok(
        await users.create(
            session,
            "admin@example.org",
            is_admin=True,
            password="secret-pass-phrase",
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    vera = ok(
        await volunteers.create(
            session, SYSTEM, "Vera", "Volunteer", "vera@example.org"
        )
    )
    vera_u, _ = ok(
        await users.create(
            session,
            "vera@example.org",
            volunteer_id=vera.id,
            password="another-pass-phrase",
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    return {"admin_u": admin.id, "vera_u": vera_u.id}


async def test_the_table_has_the_columns_and_the_search_narrows_it(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/admin/users")
        table = only(user.find(marker="accounts"))
        assert [c["name"] for c in table.columns] == [
            "email",
            "status",
            "last_login",
            "actions",
        ]
        assert table.pagination["rowsPerPage"] == 25
        rows = {r["email"]: r for r in table.rows}
        assert rows["vera@example.org"]["linked"] == "Vera Volunteer"
        assert rows["admin@example.org"]["linked"] == "not linked to a volunteer"
        assert rows["admin@example.org"]["is_admin"]
        assert rows["vera@example.org"]["status"] == "", "a settled account: no badge"
        await user.should_see("2 accounts")

        user.find(marker="accounts-search").type("vera")
        assert [r["email"] for r in only(user.find(marker="accounts")).rows] == [
            "vera@example.org"
        ]
        await user.should_see("1 of 2 accounts")
        user.find(marker="accounts-search").clear()
        user.find(marker="accounts-search").type("admin = true")
        assert [r["email"] for r in only(user.find(marker="accounts")).rows] == [
            "admin@example.org"
        ], "the query language runs over the rows"


async def test_the_menu_actions_reach_the_account(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/admin/users")
        vera = next(
            r
            for r in only(user.find(marker="accounts")).rows
            if r["id"] == ids["vera_u"]
        )
        user.find(marker="accounts").trigger("admin", vera)
        await user.should_see("Admin right granted", retries=SLOW)
        user.notify.messages.clear()

        vera = next(
            r
            for r in only(user.find(marker="accounts")).rows
            if r["id"] == ids["vera_u"]
        )
        assert vera["is_admin"], "the reloaded row says so"
        user.find(marker="accounts").trigger("active", vera)
        await user.should_see("Account disabled", retries=SLOW)

    async with db_session() as session:
        account = await users.get(session, ids["vera_u"])
        assert account is not None and account.is_admin and not account.is_active
