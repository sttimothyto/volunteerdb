"""The /teams listing: one table carrying both the hierarchy and the coverage
counts (it used to be a coverage table stacked on a separate ui.tree).

The interesting part is the blanking: the count columns render for admins and
managers, and a manager's rows outside their own subtree must carry no
headcounts at all — hiding the column client-side would still ship them.
"""

from datetime import date
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"

COUNT_FIELDS = ("leader", "second", "core", "member", "total")


async def _parish(session) -> dict[str, int]:
    """Liturgy (Lena leads) > Music, plus Hospitality and a retired team."""
    liturgy = await teams.create(session, "Liturgy")
    music = await teams.create(session, "Music", parent_team_id=liturgy.id)
    hospitality = await teams.create(session, "Hospitality")
    retired = await teams.create(session, "Retired Guild")
    await teams.update(session, retired.id, is_active=False)

    lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
    mia = await volunteers.create(session, "Mia", "Member", "mia@example.org")
    hank = await volunteers.create(session, "Hank", "Host")
    await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
    await memberships.assign(session, mia.id, music.id, TeamRole.member)
    await memberships.assign(session, hank.id, hospitality.id, TeamRole.member)

    admin = await users.create(session, "admin@example.org", is_admin=True)
    lena_u = await users.create(session, "lena@example.org", volunteer_id=lena.id)
    mia_u = await users.create(session, "mia@example.org", volunteer_id=mia.id)
    return {
        "liturgy": liturgy.id,
        "music": music.id,
        "hospitality": hospitality.id,
        "retired": retired.id,
        "admin_u": admin.id,
        "lena_u": lena_u.id,
        "mia_u": mia_u.id,
    }


def _table(user):
    return user.find(kind=ui.table).elements.pop()


async def test_teams_table_nests_and_blanks_counts_by_permission(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        table = _table(user)
        rows = {r["name"]: r for r in table.rows}

        assert [r["name"] for r in table.rows] == [
            "Hospitality",
            "Liturgy",
            "Music",
            "Retired Guild",
        ], "depth-first: Music sits directly under its parent, not in A-Z order"
        assert [r["depth"] for r in table.rows] == [0, 0, 1, 0], (
            "the child's depth is what indents it, so it must survive into the row"
        )
        assert [r["order"] for r in table.rows] == [0, 1, 2, 3], (
            "the Team column sorts on this ordinal to restore the hierarchy"
        )
        assert rows["Retired Guild"]["inactive"] is True, (
            "inactive teams were visible in the tree and must not vanish with it"
        )

        assert rows["Liturgy"]["leader"] == 1
        assert rows["Liturgy"]["gaps"] == 1, "has a leader, still wants a second"
        assert rows["Music"]["total"] == 1
        assert rows["Music"]["gap_leader"] is True
        assert rows["Retired Guild"]["total"] == "", (
            "coverage() skips inactive teams, so there is nothing to show"
        )

        # a leader sees counts on their own subtree and nothing anywhere else
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/teams")
        rows = {r["name"]: r for r in _table(user).rows}
        assert rows["Liturgy"]["total"] == 1
        assert rows["Music"]["total"] == 1, "managed_team_ids includes sub-teams"
        for field in COUNT_FIELDS:
            assert rows["Hospitality"][field] == "", (
                f"{field} for an unmanaged team reached a non-admin's browser"
            )
        assert rows["Hospitality"]["gaps"] == ""

        # a plain member gets the directory only: no count columns, no counts
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/teams")
        table = _table(user)
        assert [c["name"] for c in table.columns] == ["team"]
        assert {r["name"] for r in table.rows} == {
            "Hospitality",
            "Liturgy",
            "Music",
            "Retired Guild",
        }, "every member can still browse the team directory"
        for row in table.rows:
            for field in (*COUNT_FIELDS, "gaps"):
                assert row[field] == "", f"{field} leaked to a plain member"


async def test_home_page_controls_gated_by_full_roster_rights(database):
    """The home-page section shows for leaders (and core/admin) on the live
    team page, and not at all for plain members."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Volunteer home page")
        await user.should_see("Set home page doc")
        await user.should_not_see("Download QR Code to Public page")

        # Mia is a plain member of Music: roster names, no home-page controls
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Roster")
        await user.should_not_see("Volunteer home page")


async def test_email_list_buttons_gated_like_contact_details(database):
    """The copy/BCC buttons only appear where emails already render — for
    full-roster viewers, on the live view."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Copy email list")
        await user.should_see("Email all (BCC)")

        # Mia sees Music's roster names, never its email addresses
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Roster")
        await user.should_not_see("Copy email list")


async def test_application_form_controls_gated_like_home_page(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Application form")
        await user.should_see("Set application form")

        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Roster")
        await user.should_not_see("Application form")


async def test_interests_listed_for_managers_only(database):
    from volunteerdb.services import interest

    async with db_session() as session:
        ids = await _parish(session)
        await interest.submit(
            session,
            team_id=ids["music"],
            name="Ann Applicant",
            email="ann@example.org",
            note="I sing alto",
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        # Lena manages Music via the Liturgy subtree
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Interested people")
        await user.should_see("Ann Applicant")
        await user.should_see("I sing alto")

        # Mia is a plain member of Music: an outsider's PII stays out of reach
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Roster")
        await user.should_not_see("Interested people")
        await user.should_not_see("Ann Applicant")


async def test_published_page_link_and_qr_use_the_path_slug(database):
    """The public URL slug comes from the team's display path ("Liturgy /
    Music" → liturgy-music), not the bare team name — regression for the
    export-CSV local that used to shadow it."""
    from volunteerdb.models import TeamPage
    from volunteerdb.services import pages as page_service

    async with db_session() as session:
        ids = await _parish(session)
        await page_service.set_home_doc_url(
            session, ids["music"], "https://docs.google.com/document/d/abc123"
        )
        session.add(
            TeamPage(team_id=ids["music"], html="<p>rehearsals</p>", status="ok")
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Public page")
        await user.should_see("Download QR Code to Public page")
        link = user.find(content="Public page", kind=ui.link).elements.pop()
        assert link.props["href"] == "/ministries/liturgy-music.html", (
            "path slug, not the bare team name"
        )


async def test_asof_snapshot_announces_itself_and_offers_a_way_back(database):
    """The picker hides behind the settings icon, but a snapshot must never be
    silent: the amber banner carries its own "Back to now"."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_not_see("Read-only snapshot as of")
        await user.should_not_see("Back to now")

        # end of today is still "after now", so the live rows are in the snapshot
        today = date.today().isoformat()
        await user.open(f"/teams/{ids['liturgy']}?as_of={today}")
        await user.should_see("Read-only snapshot as of")
        await user.should_see("Back to now")
