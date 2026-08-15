"""Headless UI test: the live suggestion dropdown on both search boxes.

Typing in the simulation assigns the input's value directly, which fires the
same on_value_change handler the browser's debounced update would, so the
lookups run without a browser (see nicegui.testing.user_interaction).
"""

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

DASHBOARD_BOX = "Find volunteers or teams…"
LIST_BOX = "Search volunteers…"


async def test_dashboard_typeahead_suggests_teams_and_volunteers(database):
    async with db_session() as session:
        music = await teams.create(session, "Music")
        choir = await teams.create(session, "Alvarado Choir", parent_team_id=music.id)
        maria = await volunteers.create(
            session, "Maria", "Alvarez", "maria@example.org", "555-1234"
        )
        await memberships.assign(session, maria.id, choir.id, TeamRole.member)
        retired = await volunteers.create(session, "Pedro", "Alvarez")
        await volunteers.update(session, retired.id, is_active=False)
        await volunteers.create(session, "Bruno", "Costa")
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        member = await users.create(
            session, "member@example.org", password="test-pass-phrase"
        )
        maria_id, choir_id = maria.id, choir.id
        admin_id, member_id = admin.id, member.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        # a single character stays below the floor: no lookup, no dropdown
        await user.open("/")
        await user.should_see(DASHBOARD_BOX, retries=30)
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("A")
        await user.should_not_see("Maria Alvarez")

        # from two characters on, both categories are suggested
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("lv")
        await user.should_see("Maria Alvarez", retries=30)
        await user.should_see("Music / Alvarado Choir")  # full display path
        await user.should_see("Pedro Alvarez")  # admins see inactive rows...
        await user.should_see("inactive")  # ...badged as such
        await user.should_not_see("Bruno Costa")  # non-matching row stays out
        await user.should_see("See every match for “Alv”")

        # a fresh query replaces the previous suggestions rather than adding to them
        user.find(kind=ui.input, content=DASHBOARD_BOX).clear()
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Costa")
        await user.should_see("Bruno Costa", retries=30)
        await user.should_not_see("Maria Alvarez")

        # a query with no hits says so instead of leaving a stale list
        user.find(kind=ui.input, content=DASHBOARD_BOX).clear()
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("zzz")
        await user.should_see("Nothing found", retries=30)
        await user.should_not_see("Bruno Costa")

        # clicking a volunteer opens the side panel, as everywhere else
        user.find(kind=ui.input, content=DASHBOARD_BOX).clear()
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Alvarez")
        await user.should_see(f"suggest-volunteer-{maria_id}", retries=30)
        user.find(marker=f"suggest-volunteer-{maria_id}").click()
        await user.should_see("Email: maria@example.org", retries=30)

        # clicking a team suggestion opens its page
        await user.open("/")
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Choir")
        await user.should_see(f"suggest-team-{choir_id}", retries=30)
        user.find(marker=f"suggest-team-{choir_id}").click()
        await user.should_see("Roster", retries=30)

        # Escape closes the dropdown; returning to a box that still holds the
        # query reopens it (retyping the same text is not a value change)
        await user.open("/")
        menu = user.find(marker="suggest-menu").elements.pop()
        assert not menu.value, "closed until there is something to suggest"
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Alv")
        await user.should_see(f"suggest-volunteer-{maria_id}", retries=30)
        assert menu.value, "suggestions opened the dropdown"
        user.find(kind=ui.input, content=DASHBOARD_BOX).trigger("keydown.esc")
        assert not menu.value
        user.find(kind=ui.input, content=DASHBOARD_BOX).trigger("click")
        assert menu.value, "clicking back into the box restores the suggestions"

        # a non-admin's suggestions leave the inactive volunteer out
        await user.open(f"/login-dev/{member_id}")
        await user.open("/")
        user.find(kind=ui.input, content=DASHBOARD_BOX).type("Alv")
        await user.should_see("Maria Alvarez", retries=30)
        await user.should_not_see("Pedro Alvarez")


async def test_volunteers_page_search_box_also_suggests(database):
    async with db_session() as session:
        maria = await volunteers.create(
            session, "Maria", "Alvarez", "maria@example.org"
        )
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        maria_id, admin_id = maria.id, admin.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        await user.should_see("dev-login ok")

        await user.open("/volunteers")
        await user.should_see(LIST_BOX, retries=30)
        user.find(kind=ui.input, content=LIST_BOX).type("Alv")
        await user.should_see(f"suggest-volunteer-{maria_id}", retries=30)

        user.find(marker=f"suggest-volunteer-{maria_id}").click()
        await user.should_see("Email: maria@example.org", retries=30)
