"""Coverage report and Cytoscape graph: counts, sorting, and visibility scoping."""

from io import BytesIO

from PIL import Image

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.permissions import load_actor
from volunteerdb.services import (
    graph,
    memberships,
    photos,
    reports,
    teams,
    users,
    volunteers,
)


async def test_coverage_counts_and_hole_first_sorting(database):
    async with db_session() as session:
        altar = await teams.create(session, "Altar")
        bakers = await teams.create(session, "Bakers")
        choir = await teams.create(session, "Choir")
        await teams.create(session, "Drama")
        await teams.update(session, choir.id, is_active=False)

        lead = await volunteers.create(session, "Lea", "Der")
        second = await volunteers.create(session, "Sec", "Ond")
        member = await volunteers.create(session, "Mem", "Ber")
        await memberships.assign(session, lead.id, altar.id, TeamRole.leader)
        await memberships.assign(session, second.id, altar.id, TeamRole.second)
        await memberships.assign(session, member.id, altar.id, TeamRole.member)
        await memberships.assign(session, member.id, bakers.id, TeamRole.member)

        rows = await reports.coverage(session)

        assert [r.team.name for r in rows] == ["Bakers", "Drama", "Altar"], (
            "holes first, inactive Choir excluded"
        )
        by_name = {r.team.name: r for r in rows}
        assert by_name["Altar"].counts == {
            TeamRole.leader: 1,
            TeamRole.second: 1,
            TeamRole.member: 1,
        }
        assert by_name["Altar"].total == 3
        assert (
            not by_name["Altar"].missing_leader and not by_name["Altar"].missing_second
        )
        assert by_name["Bakers"].missing_leader and by_name["Bakers"].missing_second
        assert by_name["Drama"].total == 0


async def _parish(session):
    """Parent team with a sub-team and an unrelated team, one volunteer each."""
    parent = await teams.create(session, "Liturgy")
    child = await teams.create(session, "Music", parent_team_id=parent.id)
    other = await teams.create(session, "Hospitality")

    on_parent = await volunteers.create(session, "Pat", "Parent")
    on_child = await volunteers.create(session, "Chris", "Child")
    on_other = await volunteers.create(session, "Ollie", "Other")
    await memberships.assign(session, on_parent.id, parent.id, TeamRole.leader)
    await memberships.assign(session, on_child.id, child.id, TeamRole.member)
    await memberships.assign(session, on_other.id, other.id, TeamRole.leader)
    return parent, child, other, on_parent, on_child, on_other


async def test_graph_admin_sees_everything(database):
    async with db_session() as session:
        parent, child, other, on_parent, on_child, on_other = await _parish(session)
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        actor = await load_actor(session, admin)

        data = await graph.elements(session, actor)

        node_ids = {n["data"]["id"] for n in data["nodes"]}
        assert {f"t{parent.id}", f"t{child.id}", f"t{other.id}"} <= node_ids
        assert {f"v{on_parent.id}", f"v{on_child.id}", f"v{on_other.id}"} <= node_ids

        edges = {e["data"]["id"]: e["data"] for e in data["edges"]}
        assert edges[f"m{parent.id}-{on_parent.id}"]["leadership"] is True
        assert edges[f"m{child.id}-{on_child.id}"]["leadership"] is False
        assert edges[f"h{child.id}"]["hierarchy"] is True, "sub-team edge present"

        leader_node = next(
            n for n in data["nodes"] if n["data"]["id"] == f"v{on_parent.id}"
        )
        assert "band" in leader_node["data"], "admins see workload colouring"


async def test_graph_member_sees_only_own_team(database):
    async with db_session() as session:
        parent, child, other, on_parent, on_child, on_other = await _parish(session)
        account = await users.create(
            session,
            "chris@example.org",
            volunteer_id=on_child.id,
            password="test-pass-phrase",
        )
        actor = await load_actor(session, account)

        data = await graph.elements(session, actor)

        node_ids = {n["data"]["id"] for n in data["nodes"]}
        assert node_ids == {f"t{child.id}", f"v{on_child.id}"}, (
            "a plain member sees only their direct team"
        )
        edge_ids = {e["data"]["id"] for e in data["edges"]}
        assert f"h{child.id}" not in edge_ids, (
            "hierarchy edge dropped: parent invisible"
        )

        own_node = next(
            n for n in data["nodes"] if n["data"]["id"] == f"v{on_child.id}"
        )
        assert "band" not in own_node["data"], "workload is a leadership-only signal"


async def test_graph_photo_datum_only_on_photographed_volunteers(database):
    async with db_session() as session:
        parent, child, other, on_parent, on_child, on_other = await _parish(session)
        buffer = BytesIO()
        Image.new("RGB", (500, 500), (50, 100, 150)).save(buffer, format="PNG")
        await photos.set_photo(
            session, on_parent.id, buffer.getvalue(), uploaded_by=None
        )
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        actor = await load_actor(session, admin)

        data = await graph.elements(session, actor)

        by_id = {n["data"]["id"]: n["data"] for n in data["nodes"]}
        assert by_id[f"v{on_parent.id}"]["photo"].startswith(
            f"/photos/{on_parent.id}?v="
        ), "photographed volunteers carry their cookie-authed image URL"
        assert "photo" not in by_id[f"v{on_child.id}"], "no photo, no datum"


async def test_graph_team_filter_restricts_to_subtree(database):
    async with db_session() as session:
        parent, child, other, on_parent, on_child, on_other = await _parish(session)
        admin = await users.create(
            session, "admin@example.org", is_admin=True, password="test-pass-phrase"
        )
        actor = await load_actor(session, admin)

        data = await graph.elements(session, actor, team_id=parent.id)

        node_ids = {n["data"]["id"] for n in data["nodes"]}
        assert f"t{other.id}" not in node_ids and f"v{on_other.id}" not in node_ids
        assert {
            f"t{parent.id}",
            f"t{child.id}",
            f"v{on_parent.id}",
            f"v{on_child.id}",
        } <= node_ids
        assert f"h{child.id}" in {e["data"]["id"] for e in data["edges"]}
