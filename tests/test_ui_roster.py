"""The roster is a table.

One row per member with the columns a reader may see, sortable, searchable,
25 to a page: a 59-member team was the site's longest page as one row of
widgets per member. The contact columns exist only for full-roster viewers
(a column the browser does not draw still receives its data), and hide on a
phone (theme.css .vdb-col-wide); the role and invite controls are one dialog
on demand and one button per row, emitting events the tests can fire."""

from datetime import UTC, datetime

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb import timefmt
from volunteerdb.env import current
from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


async def _parish(session) -> dict[str, int]:
    """Music: Lena leads, Cora is core, Mia and thirty more are members."""
    music = ok(await teams.create(session, SYSTEM, "Music"))
    people = {}
    for first, last, role in (
        ("Lena", "Leader", TeamRole.leader),
        ("Cora", "Core", TeamRole.core),
        ("Mia", "Member", TeamRole.member),
    ):
        v = ok(
            await volunteers.create(
                session, SYSTEM, first, last, f"{first.lower()}@example.org", "555-0100"
            )
        )
        ok(await memberships.assign(session, SYSTEM, v.id, music.id, role))
        people[first.lower()] = v
    for n in range(30):
        v = ok(await volunteers.create(session, SYSTEM, f"Member{n:02d}", "Extra"))
        ok(await memberships.assign(session, SYSTEM, v.id, music.id, TeamRole.member))
    accounts = {}
    for key in ("lena", "cora", "mia"):
        u, _ = ok(
            await users.create(
                session,
                f"{key}@example.org",
                volunteer_id=people[key].id,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        u.password_hash = "x"  # settled accounts: no invite to offer
        accounts[key] = u.id
    await session.flush()
    return {"music": music.id, **{f"{k}_u": v for k, v in accounts.items()}}


def _table(user) -> ui.table:
    return only(user.find(marker="roster"))


async def test_each_tier_gets_the_columns_it_may_see(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        # a member: names, roles, accounts, since -- no contact details, no actions
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        table = _table(user)
        assert [c["name"] for c in table.columns] == [
            "name",
            "role",
            "account",
            "since",
        ]
        assert "email" not in table.rows[0], "the data never reaches her browser"
        assert table.pagination["rowsPerPage"] == 25
        assert len(table.rows) == 33, "every row is in the table; the page shows 25"

        # a core member: the contact columns, marked for the phone to hide
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open(f"/teams/{ids['music']}")
        table = _table(user)
        names = [c["name"] for c in table.columns]
        assert names == ["name", "role", "email", "phone", "account", "since"]
        wide = {c["name"] for c in table.columns if c.get("classes") == "vdb-col-wide"}
        assert wide == {"email", "phone"}
        assert table.rows[0]["email"] == "lena@example.org"
        assert table.rows[0]["invite"] == "", "a settled account offers no invite"

        # the leader: the actions column too, and the role on every row is a button
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        table = _table(user)
        assert [c["name"] for c in table.columns][-1] == "actions"
        assert all(r["can_manage"] for r in table.rows)
        assert [r["role_label"] for r in table.rows[:3]] == [
            "Ministry leader",
            "Core team member",
            "Member",
        ], "leader first: the rows come in role order and the column sorts on it"


async def test_since_is_the_day_the_membership_began(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        today = timefmt.day(datetime.now(UTC), current().tz)
        assert {r["since"] for r in _table(user).rows} == {today}


async def test_the_search_box_narrows_the_roster(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("33 members")
        user.find(marker="roster-search").type("cora")
        assert [r["name"] for r in _table(user).rows] == ["Cora Core"]
        await user.should_see("1 of 33 members")
        user.find(marker="roster-search").clear()
        user.find(marker="roster-search").type("role = 'Member'")
        assert len(_table(user).rows) == 31, "the query language works on the rows"
