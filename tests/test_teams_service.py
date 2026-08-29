"""Team service: pure tree helpers, cycle prevention, update sentinel semantics."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from volunteerdb.actors import load_actor
from volunteerdb.db import db_session
from volunteerdb.models import Team, TeamRole, TeamSheet
from volunteerdb.permissions import Forbidden
from volunteerdb.services import memberships, teams, users, volunteers
from volunteerdb.services.teams import CycleError


def _tree() -> list[Team]:
    return [
        Team(id=1, name="Alpha", parent_team_id=None),
        Team(id=2, name="beta", parent_team_id=1),
        Team(id=3, name="Gamma", parent_team_id=2),
        Team(id=4, name="Delta", parent_team_id=None),
    ]


def test_children_map_sorts_siblings_case_insensitively():
    siblings = [
        Team(id=1, name="zeta", parent_team_id=None),
        Team(id=2, name="Alpha", parent_team_id=None),
        Team(id=3, name="miDDle", parent_team_id=None),
    ]
    by_parent = teams.children_map(siblings)
    assert [t.name for t in by_parent[None]] == ["Alpha", "miDDle", "zeta"]


def test_descendants_includes_root_and_transitive():
    tree = teams.build_tree(_tree())
    assert tree.descendants(1) == {1, 2, 3}
    assert tree.descendants(2) == {2, 3}
    assert tree.descendants(4) == {4}
    assert tree.descendants(99) == {99}, "unknown root is just itself"


def test_team_paths_builds_parent_slash_child():
    paths = teams.team_paths(_tree())
    assert paths == {
        1: "Alpha",
        2: "Alpha / beta",
        3: "Alpha / beta / Gamma",
        4: "Delta",
    }


def test_team_paths_terminates_on_a_cycle():
    """team_paths runs on as-of snapshots that no service call has validated
    (exporter, graph, teams page), and the failure mode is a RecursionError that
    takes the whole request down rather than a bad path string."""
    cyclic = [
        Team(id=1, name="A", parent_team_id=2),
        Team(id=2, name="B", parent_team_id=1),
    ]
    paths = teams.team_paths(cyclic)
    assert set(paths) == {1, 2}, "every team still gets a path"


async def test_create_rejects_a_negative_workload_weight(database):
    """update validates the weight; create must too, or POST /api/teams accepts
    what PATCH refuses and the negative flows into every workload band."""
    async with db_session() as session:
        with pytest.raises(ValueError):
            await teams.create(session, None, "Negative", workload_weight=Decimal("-2"))

        ok = await teams.create(session, None, "Fine", workload_weight=Decimal("2.5"))
        assert ok.workload_weight == Decimal("2.5")

        unweighted = await teams.create(session, None, "Unweighted")
        assert unweighted.workload_weight == Decimal(0), (
            "a team with no weight given weighs 0 — which is what excluding it "
            "from the scores has always meant (models.Team.workload_weight)"
        )


async def test_update_reparent_cycle_rejected(database):
    async with db_session() as session:
        a = await teams.create(session, None, "A")
        b = await teams.create(session, None, "B", parent_team_id=a.id)
        c = await teams.create(session, None, "C", parent_team_id=b.id)

        with pytest.raises(CycleError):
            await teams.update(session, None, a.id, parent_team_id=c.id)
        with pytest.raises(CycleError):
            await teams.update(session, None, a.id, parent_team_id=a.id)

        # a legal reparent within the same tree still works
        moved = await teams.update(session, None, c.id, parent_team_id=a.id)
        assert moved.parent_team_id == a.id


async def test_update_unset_vs_explicit_none_parent(database):
    async with db_session() as session:
        parent = await teams.create(session, None, "Parent")
        child = await teams.create(session, None, "Child", parent_team_id=parent.id)

        renamed = await teams.update(session, None, child.id, name="Renamed")
        assert renamed.parent_team_id == parent.id, "omitted parent stays untouched"

        orphaned = await teams.update(session, None, child.id, parent_team_id=None)
        assert orphaned.parent_team_id is None, "explicit None detaches"


async def test_update_workload_weight_validation(database):
    async with db_session() as session:
        team = await teams.create(session, None, "Weighted")

        with pytest.raises(ValueError):
            await teams.update(session, None, team.id, workload_weight=Decimal("-1"))

        updated = await teams.update(
            session, None, team.id, workload_weight=Decimal("2.5")
        )
        assert updated.workload_weight == Decimal("2.5")

        cleared = await teams.update(session, None, team.id, workload_weight=None)
        assert cleared.workload_weight == Decimal(0), "clearing means 0, not NULL"


async def test_two_top_level_teams_cannot_share_a_name(database):
    """The unique constraint uses NULLS NOT DISTINCT (Postgres 15+), so two
    parentless teams cannot share a name. Without it a duplicate 'Music' would
    make the importer's team-path lookup ambiguous for every future import."""
    async with db_session() as session:
        await teams.create(session, None, "Music")
        with pytest.raises(IntegrityError):
            await teams.create(session, None, "Music")

    async with db_session() as session:
        liturgy = await teams.create(session, None, "Liturgy")
        youth = await teams.create(session, None, "Youth")
        await teams.create(session, None, "Music", parent_team_id=liturgy.id)
        await teams.create(
            session, None, "Music", parent_team_id=youth.id
        )  # different parents: fine


async def test_missing_team_raises_lookup(database):
    async with db_session() as session:
        assert await teams.get(session, 424242) is None
        with pytest.raises(LookupError):
            await teams.update(session, None, 424242, name="X")
        with pytest.raises(LookupError):
            await teams.delete(session, None, 424242)


async def test_search_matches_name_description_and_path(database):
    async with db_session() as session:
        liturgy = await teams.create(session, None, "Liturgy")
        await teams.create(
            session,
            None,
            "Altar Servers",
            parent_team_id=liturgy.id,
            description="robes and candles",
        )
        retired = await teams.create(session, None, "Old Guild")
        await teams.update(session, None, retired.id, is_active=False)

        assert [t.name for t, _ in await teams.search(session, "altar")] == [
            "Altar Servers"
        ]
        assert [t.name for t, _ in await teams.search(session, "candles")] == [
            "Altar Servers"
        ], "description matches"
        hits = await teams.search(session, "liturgy")
        assert [(t.name, path) for t, path in hits] == [
            ("Liturgy", "Liturgy"),
            ("Altar Servers", "Liturgy / Altar Servers"),
        ], "a parent's name matches its children through the path, sorted by path"
        assert await teams.search(session, "old guild") == [], "inactive teams excluded"
        assert await teams.search(session, "   ") == [], "blank query matches nothing"


async def test_leader_emails_covers_leader_and_second_only(database):
    """Who the nightly Drive sync shares a roster sheet with, and who an event's
    notices go to. Core members are not leadership, and an address a mailer
    cannot use is not an address."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        team_id = team.id
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        sam = await volunteers.create(session, None, "Sam", "Second", "sam@example.org")
        cora = await volunteers.create(
            session, None, "Cora", "Core", "cora@example.org"
        )
        noel = await volunteers.create(session, None, "Noel", "NoEmail")
        # what an import of a roster with an empty Email cell can leave behind:
        # not NULL, but nothing a mailer or a Drive share can use either
        blank = await volunteers.create(session, None, "Bea", "Blank")
        blank.email = "  "
        await memberships.assign(session, None, lena.id, team_id, TeamRole.leader)
        await memberships.assign(session, None, sam.id, team_id, TeamRole.second)
        await memberships.assign(session, None, cora.id, team_id, TeamRole.core)
        await memberships.assign(session, None, noel.id, team_id, TeamRole.second)
        await memberships.assign(session, None, blank.id, team_id, TeamRole.leader)
        assert await teams.leader_emails(session, team_id) == [
            "lena@example.org",
            "sam@example.org",
        ]


async def test_set_roster_sheet_validates_the_link(database):
    """Only the link's shape is checked here; whether the FILE is shared and
    shaped like a roster is the sync's to discover, and it says so in the
    error it records against the team."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")

        with pytest.raises(ValueError, match="Google Sheets link"):
            await teams.set_roster_sheet(session, None, team.id, "not a url")
        with pytest.raises(ValueError, match="Google Sheets link"):
            # a Doc link is the mistake worth catching: the team page offers a
            # box for each, and they are not interchangeable
            await teams.set_roster_sheet(
                session, None, team.id, "https://docs.google.com/document/d/abc123"
            )

        sheet = await teams.set_roster_sheet(
            session,
            None,
            team.id,
            "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0",
        )
        assert sheet.file_id == "abc123", "the id, not the whole URL"


async def test_a_leader_may_set_the_roster_sheet(database):
    """Widened from admin-only with the transport. The old rule was not a
    judgement about trust: nothing in the app could reach Drive, so a leader's
    pasted link could not be checked until the next nightly run."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        await memberships.assign(session, None, lena.id, team.id, TeamRole.leader)
        user, _ = await users.create(session, "lena@example.org")
        leader = await load_actor(session, user)

    async with db_session() as session:
        sheet = await teams.set_roster_sheet(
            session, leader, team.id, "https://docs.google.com/spreadsheets/d/abc123"
        )
        assert sheet.file_id == "abc123"


async def test_a_core_member_may_not_set_the_roster_sheet(database):
    """Where this stops, and why it stops short of the home-page doc: that
    publishes a page anybody may read, while this carries every member's
    address and phone and grants a bulk write over the roster."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        cora = await volunteers.create(
            session, None, "Cora", "Core", "cora@example.org"
        )
        await memberships.assign(session, None, cora.id, team.id, TeamRole.core)
        user, _ = await users.create(session, "cora@example.org")
        core = await load_actor(session, user)

    async with db_session() as session:
        with pytest.raises(Forbidden):
            await teams.set_roster_sheet(
                session, core, team.id, "https://docs.google.com/spreadsheets/d/abc123"
            )


async def test_one_spreadsheet_belongs_to_one_team(database):
    """Two teams on one sheet would each overwrite the other's roster every
    night, so a sheet already in use is refused."""
    async with db_session() as session:
        choir = await teams.create(session, None, "Choir")
        altar = await teams.create(session, None, "Altar Society")
        url = "https://docs.google.com/spreadsheets/d/abc123"

        await teams.set_roster_sheet(session, None, choir.id, url)
        with pytest.raises(ValueError, match="Choir"):
            await teams.set_roster_sheet(session, None, altar.id, url)


async def test_setting_the_sheet_the_team_already_has_is_refused(database):
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        session.add(TeamSheet(team_id=team.id, file_id="abc123"))
        await session.flush()

        with pytest.raises(ValueError, match="already syncs"):
            await teams.set_roster_sheet(
                session, None, team.id, "https://docs.google.com/spreadsheets/d/abc123"
            )


async def test_relinking_clears_the_previous_sync_result(database):
    """A stale 'Last sync failed' under a link that has since been replaced
    reads as a problem with the NEW sheet."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        session.add(
            TeamSheet(
                team_id=team.id,
                file_id="old123",
                file_name="choir-membership-list",
                last_status="error",
                last_error="the spreadsheet is not shared",
            )
        )
        await session.flush()

        sheet = await teams.set_roster_sheet(
            session, None, team.id, "https://docs.google.com/spreadsheets/d/new456"
        )
        assert sheet.file_id == "new456"
        assert sheet.last_status is None
        assert sheet.last_error is None
        assert sheet.file_name is None, "the sync fills this in from the sheet"


async def test_roster_sheet_needs_management_rights(database):
    """Who may see the link is who may manage the roster: the sheet IS the
    roster, contact details and all."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        mia = await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
        await memberships.assign(session, None, mia.id, team.id, TeamRole.member)
        user, _ = await users.create(session, "mia@example.org")
        member = await load_actor(session, user)

    async with db_session() as session:
        with pytest.raises(Forbidden):
            await teams.roster_sheet(session, member, team.id)
        assert await teams.roster_sheet(session, None, team.id) is None
