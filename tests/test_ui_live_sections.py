"""Four sections refresh in place (uiux-improvement.md, step 21).

A role change, an assignment, a tick and a score used to reload the page:
the scroll, the sort, the search box and the toast went with it. Now each
redraws its own block from a fresh read (ui/context.py: live, and the
roster's in-place rows in teams_page). The proof of "in place" here is the
page's own help button: the same element object is still on the page once
the command is done, which a reload -- a new page, new elements -- cannot
leave standing.
"""

import asyncio
from datetime import timedelta

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import elections, memberships, teams, users, volunteers
from volunteerdb.services import events as event_service

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok
from tests.test_ui_elections import _parish as _election_parish
from tests.test_ui_elections import _seed_proposal
from tests.test_ui_events import _parish as _event_parish
from tests.test_ui_events import _seed_event


async def _until(holds) -> None:
    for _ in range(SLOW):
        if holds():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the section never caught up")


def _help(user) -> ui.element:
    """The frame's help button: one per page, made with the page."""
    return only(user.find(marker="page-help"))


async def test_a_role_change_keeps_the_search_and_the_page(database):
    async with db_session() as session:
        music = ok(await teams.create(session, SYSTEM, "Music"))
        lena = ok(
            await volunteers.create(
                session, SYSTEM, "Lena", "Leader", "lena@example.org"
            )
        )
        mia = ok(await volunteers.create(session, SYSTEM, "Mia", "Member"))
        nils = ok(await volunteers.create(session, SYSTEM, "Nils", "Nobody"))
        ok(
            await memberships.assign(
                session, SYSTEM, lena.id, music.id, TeamRole.leader
            )
        )
        mia_m = ok(
            await memberships.assign(session, SYSTEM, mia.id, music.id, TeamRole.member)
        )
        ok(
            await memberships.assign(
                session, SYSTEM, nils.id, music.id, TeamRole.member
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
        ids = {"music": music.id, "mia_m": mia_m.id, "lena_u": lena_u.id}

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        page = _help(user)
        search = only(user.find(marker="roster-search"))
        search.value = "mia"
        table = only(user.find(marker="roster"))
        assert [r["name"] for r in table.rows] == ["Mia Member"], "narrowed"

        row = next(r for r in table.every if r["id"] == ids["mia_m"])
        user.find(marker="roster").trigger("role", row)
        await user.should_see("Change the role of Mia Member", retries=SLOW)
        only(user.find(marker="role-pick")).set_value("core")
        user.find(marker="role-save").click()
        await user.should_see("Role updated", retries=SLOW)

        # the same page, the same table, the same search -- with the new role
        assert not page.is_deleted and _help(user) is page
        assert only(user.find(marker="roster")) is table
        assert only(user.find(marker="roster-search")) is search
        assert search.value == "mia"
        await _until(lambda: [r["role"] for r in table.rows] == ["core"])
        assert len(table.every) == 3, "the whole roster is behind the search"


async def test_a_sign_up_redraws_the_slots_in_place(database):
    async with db_session() as session:
        ids = await _event_parish(session)
    event_id = await _seed_event(
        ids["liturgy"], slots=[event_service.SlotInput("Lector", 2)]
    )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{event_id}")
        page = _help(user)
        await user.should_see("0/2")
        user.find("Sign up", kind=ui.button).click()
        user.find(marker="signup-confirm").click()
        await user.should_see("You're on the list", retries=SLOW)
        await user.should_see("1/2", retries=SLOW)
        await user.should_see("Mia Member")
        assert not page.is_deleted and _help(user) is page
        await user.should_not_see("Sign up", retries=SLOW)


async def test_an_attendance_tick_redraws_the_list_in_place(database):
    async with db_session() as session:
        ids = await _event_parish(session)
    event_id = await _seed_event(ids["liturgy"])
    async with db_session() as session:
        view = ok(await event_service.detail(session, SYSTEM, event_id))
        ok(
            await event_service.assign(
                session,
                SYSTEM,
                slot_id=view.slots[0].slot.id,
                volunteer_id=ids["mia"],
                assigned_by=ids["lena_u"],
                now=mint.now(),
            )
        )
        past = mint.now() - timedelta(days=2)
        ok(
            await event_service.update_event(
                session,
                SYSTEM,
                event_id,
                starts_at=past,
                ends_at=past + timedelta(hours=2),
            )
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        page = _help(user)
        await user.should_see("Attendance")
        await user.should_not_see("adjusted")
        only(user.find(kind=ui.checkbox, content="attended")).value = False
        user.find("Save", kind=ui.button).click()
        await user.should_see("Attendance saved", retries=SLOW)
        await user.should_see("adjusted", retries=SLOW)
        await user.should_see("Reset", retries=SLOW)
        assert not page.is_deleted and _help(user) is page


async def test_a_voter_and_a_ballot_redraw_the_roll_in_place(database):
    async with db_session() as session:
        ids = await _election_parish(session)
    # nominations open: the admin adds a voter and sees the roll grow
    open_pid = await _seed_proposal(ids, d1_offset=5, d2_offset=15)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/elections/{open_pid}")
        page = _help(user)
        await user.should_see("of 3 ballots cast")
        only(user.find(kind=ui.select, content="Add a voter")).value = ids["vera"]
        user.find("Add voter", kind=ui.button).click()
        await user.should_see("Voter added", retries=SLOW)
        await user.should_see("of 4 ballots cast", retries=SLOW)
        assert not page.is_deleted and _help(user) is page

    # voting open: a ballot ticks the turnout up on the same page
    async with db_session() as session:
        ok(
            await elections.cancel(
                session, SYSTEM, open_pid, decided_by=ids["admin_u"], now=mint.now()
            )
        )
    voting_pid = await _seed_proposal(ids, d1_offset=-1, d2_offset=6)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open(f"/elections/{voting_pid}")
        page = _help(user)
        await user.should_see("0 of 3 ballots cast")
        only(user.find(kind=ui.toggle)).value = 5
        user.find("Submit ballot", kind=ui.button).click()
        await user.should_see("Ballot recorded", retries=SLOW)
        await user.should_see("1 of 3 ballots cast", retries=SLOW)
        assert not page.is_deleted and _help(user) is page
