"""The /teams listing: one table carrying both the hierarchy and the coverage
counts (it used to be a coverage table stacked on a separate ui.tree).

The interesting part is the blanking: the count columns render for admins and
managers, and a manager's rows outside their own subtree must carry no
headcounts at all — hiding the column client-side would still ship them.
"""

from decimal import Decimal

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamPage, TeamRole, TeamSheet
from volunteerdb.services import memberships, pages, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok

COUNT_FIELDS = ("leader", "second", "core", "member", "total")
SEARCH_BOX = "Search teams…"


async def _parish(session) -> dict[str, int]:
    """Liturgy (Lena leads) > Music, plus Hospitality and a retired team."""
    liturgy = ok(await teams.create(session, None, "Liturgy"))
    music = ok(await teams.create(session, None, "Music", parent_team_id=liturgy.id))
    hospitality = ok(await teams.create(session, None, "Hospitality"))
    retired = ok(await teams.create(session, None, "Retired Guild"))
    ok(await teams.update(session, None, retired.id, is_active=False))

    lena = ok(
        await volunteers.create(session, None, "Lena", "Leader", "lena@example.org")
    )
    mia = ok(await volunteers.create(session, None, "Mia", "Member", "mia@example.org"))
    hank = ok(await volunteers.create(session, None, "Hank", "Host"))
    ok(await memberships.assign(session, None, lena.id, liturgy.id, TeamRole.leader))
    ok(await memberships.assign(session, None, mia.id, music.id, TeamRole.member))
    ok(
        await memberships.assign(
            session, None, hank.id, hospitality.id, TeamRole.member
        )
    )

    admin, _ = ok(
        await users.create(
            session, "admin@example.org", is_admin=True, invite=mint.fresh_invite()
        )
    )
    lena_u, _ = ok(
        await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            invite=mint.fresh_invite(),
        )
    )
    mia_u, _ = ok(
        await users.create(
            session, "mia@example.org", volunteer_id=mia.id, invite=mint.fresh_invite()
        )
    )
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
    return only(user.find(kind=ui.table))


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


async def test_search_narrows_the_table_without_orphaning_children(database):
    """The box filters the rows already in the browser. It matches the display
    path, so a parent's name brings its subtree along, and it keeps every
    match's ancestors, so a child never renders indented under nothing."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        await user.should_see(SEARCH_BOX)
        await user.should_see("4 teams")

        # a child's own name keeps the parent it hangs off
        user.find(kind=ui.input, content=SEARCH_BOX).type("Music")
        assert [r["name"] for r in _table(user).rows] == ["Liturgy", "Music"]
        await user.should_see("2 of 4 teams")

        # the parent's name matches every path beneath it
        user.find(kind=ui.input, content=SEARCH_BOX).clear()
        user.find(kind=ui.input, content=SEARCH_BOX).type("litur")
        assert [r["name"] for r in _table(user).rows] == ["Liturgy", "Music"]

        user.find(kind=ui.input, content=SEARCH_BOX).clear()
        user.find(kind=ui.input, content=SEARCH_BOX).type("zzz")
        assert _table(user).rows == []
        await user.should_see("0 of 4 teams")

        # clearing restores the whole parish, in hierarchy order
        user.find(kind=ui.input, content=SEARCH_BOX).clear()
        assert [r["name"] for r in _table(user).rows] == [
            "Hospitality",
            "Liturgy",
            "Music",
            "Retired Guild",
        ]
        await user.should_see("4 teams")


async def test_every_column_sorts(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        columns = _table(user).columns
        assert [c["name"] for c in columns] == [
            "team",
            "leader",
            "second",
            "core",
            "member",
            "total",
            "gaps",
        ]
        assert all(c.get("sortable") for c in columns), (
            f"not sortable: {[c['name'] for c in columns if not c.get('sortable')]}"
        )


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


async def _publish(session, team_id: int) -> None:
    """Give a team a live public page: a doc link plus cached html."""
    ok(
        await pages.set_home_doc_url(
            session, None, team_id, f"https://docs.google.com/document/d/x{team_id}"
        )
    )
    session.add(TeamPage(team_id=team_id, html="<p>hello</p>", status="ok"))


async def test_the_teams_list_offers_the_public_index_to_everyone(database):
    """The only door from inside the app to /ministries/ — no permission gate,
    because the index is world-readable anyway."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/teams")
        await user.should_see("View Team Homepages", retries=SLOW)


async def test_a_non_member_reaches_a_published_team_page(database):
    """A team's public page is public: the link shows for a reader who is not
    on its roster and cannot see a single name on it."""
    async with db_session() as session:
        ids = await _parish(session)
        await _publish(session, ids["hospitality"])

    async with user_simulation(main_file=SIM_MAIN) as user:
        # Mia is on Music only — Hospitality's roster is closed to her
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['hospitality']}")
        await user.should_see("not on this team", retries=SLOW)
        await user.should_see("View public homepage")

        # ... and there is nothing to link to on a team that never published
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("not on this team", retries=SLOW)
        await user.should_not_see("View public homepage")


async def test_full_roster_viewers_get_the_public_link_once(database):
    """A leader already reaches the page from the Volunteer home page section,
    so the action-row link must not double up."""
    async with db_session() as session:
        ids = await _parish(session)
        await _publish(session, ids["liturgy"])

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Public page", retries=SLOW)
        await user.should_not_see("View public homepage")


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


async def test_published_page_link_and_qr_use_the_path_slug(database):
    """The public URL slug comes from the team's display path ("Liturgy /
    Music" → liturgy-music), not the bare team name — regression for the
    export-CSV local that used to shadow it."""
    from volunteerdb.models import TeamPage
    from volunteerdb.services import pages as page_service

    async with db_session() as session:
        ids = await _parish(session)
        ok(
            await page_service.set_home_doc_url(
                session, None, ids["music"], "https://docs.google.com/document/d/abc123"
            )
        )
        session.add(
            TeamPage(team_id=ids["music"], html="<p>rehearsals</p>", status="ok")
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Public page")
        await user.should_see("Download QR Code to Public page")
        link = only(user.find(content="Public page", kind=ui.link))
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
        today = mint.today().isoformat()
        await user.open(f"/teams/{ids['liturgy']}?as_of={today}")
        await user.should_see("Read-only snapshot as of")
        await user.should_see("Back to now")


async def test_search_box_runs_where_filters_over_the_rows(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        await user.should_see(SEARCH_BOX)

        box = user.find(kind=ui.input, content=SEARCH_BOX)
        box.type("total > 0 AND name ILIKE 'm%'")
        assert [r["name"] for r in _table(user).rows] == ["Liturgy", "Music"], (
            "query matches keep their ancestors, like substring matches"
        )
        await user.should_see("2 of 4 teams")

        box.clear()
        box.type("gaps = 2")
        assert [r["name"] for r in _table(user).rows] == [
            "Hospitality",
            "Liturgy",
            "Music",
        ], "Liturgy rides along only as Music's ancestor"

        # a typo'd field reports inline instead of filtering
        box.clear()
        box.type("bogus = 1")
        await user.should_see("query error")

        # blanked counts never match for viewers without manage rights
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/teams")
        user.find(kind=ui.input, content=SEARCH_BOX).type("total > 0")
        assert _table(user).rows == []
        await user.should_see("0 of 4 teams")


async def test_roster_sheet_section_gated_by_role(database):
    """The sheet IS the roster, contact details and all — and holding its link
    is edit access to it — so the whole section shows to managers only. Within
    it, leaders now get every control an admin gets."""
    async with db_session() as session:
        ids = await _parish(session)
        session.add(
            TeamSheet(team_id=ids["liturgy"], file_id="f123", file_name="liturgy-list")
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Roster spreadsheet")
        await user.should_see("liturgy-list")
        await user.should_see("Change spreadsheet")

        # Lena leads Liturgy: same controls as the admin, including the import
        # that used to live on its own page
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Roster spreadsheet")
        await user.should_see("liturgy-list")
        await user.should_see("Change spreadsheet")
        await user.should_see("Import a .csv")
        await user.should_see("DO NOT edit the ID Column")

        # Mia is a plain member of Music: no sheet section at all
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/teams/{ids['music']}")
        await user.should_see("Roster")
        await user.should_not_see("Roster spreadsheet")
        await user.should_not_see("Import a .csv")


async def test_the_change_dialog_warns_that_the_link_is_the_access(database):
    """The one thing a leader must be told: link sharing means whoever holds
    the link can rewrite the roster."""
    async with db_session() as session:
        ids = await _parish(session)
        session.add(TeamSheet(team_id=ids["liturgy"], file_id="f123"))

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        user.find("Change spreadsheet", kind=ui.button).click()
        await user.should_see("Keep this link private")
        await user.should_see("anyone with the link")


async def test_the_change_dialog_rejects_a_doc_link(database):
    """The team page offers a box for each, and they are not interchangeable."""
    async with db_session() as session:
        ids = await _parish(session)
        session.add(TeamSheet(team_id=ids["liturgy"], file_id="f123"))

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['liturgy']}")
        user.find("Change spreadsheet", kind=ui.button).click()
        await user.should_see("Google Sheets link")
        user.find("Google Sheets link").type(
            "https://docs.google.com/document/d/abc123"
        )
        user.find("Save", kind=ui.button).click()
        # notify_errors turns the service's ValueError into a toast, and leaves
        # the dialog open on the bad value
        await user.should_see("not a Google Sheets link", retries=SLOW)

    async with db_session() as session:
        sheet = await session.get(TeamSheet, ids["liturgy"])
    assert sheet.file_id == "f123", "a rejected link costs the team nothing"


async def test_export_teams_button_is_access_control_aware(database):
    """Replaces the retired /import page's parish/my-teams pair. A plain
    member has nothing to export, so the button is not rendered at all —
    the exporter would refuse the scope anyway, this only keeps an unusable
    button off the screen."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        await user.should_see("Export team(s)")

        # Lena leads Liturgy
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/teams")
        await user.should_see("Export team(s)")

        # Mia is a plain member of Music: no teams of her own to export
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/teams")
        await user.should_not_see("Export team(s)")


async def test_a_core_member_may_export_their_teams(database):
    """full_view_team_ids, not people_team_ids: core is included on purpose,
    and the exporter blanks the notes column for them."""
    async with db_session() as session:
        ids = await _parish(session)
        cora = ok(
            await volunteers.create(session, None, "Cora", "Core", "cora@example.org")
        )
        ok(
            await memberships.assign(
                session, None, cora.id, ids["hospitality"], TeamRole.core
            )
        )
        cora_u, _ = ok(
            await users.create(
                session,
                "cora@example.org",
                volunteer_id=cora.id,
                invite=mint.fresh_invite(),
            )
        )
        ids["cora_u"] = cora_u.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open("/teams")
        await user.should_see("Export team(s)")
        # but the roster spreadsheet stays with leaders and seconds
        await user.open(f"/teams/{ids['hospitality']}")
        await user.should_not_see("Roster spreadsheet")


async def test_new_team_dialog_starts_at_weight_one(database):
    """A new ministry is ordinary work, not zero work — and 0, the old default,
    is what excludes a team from workload scores altogether."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        user.find("New team", kind=ui.button).click()
        await user.should_see("Workload weight")
        weight = only(user.find(kind=ui.number))
        assert weight.value == 1.0, "the box opens at 1, not empty"

        user.find("Name").type("Sacristans")
        user.find("Save", kind=ui.button).click()
        # wait on the team DETAIL page the save navigates to, not on the name —
        # the dialog's own input still shows that while the save is in flight
        await user.should_see("Volunteer home page", retries=SLOW)

    async with db_session() as session:
        (created,) = [
            t for t in (await teams.tree(session)).teams if t.name == "Sacristans"
        ]
    assert created.workload_weight == Decimal(1)
