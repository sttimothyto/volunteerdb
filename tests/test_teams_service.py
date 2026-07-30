"""Team service: pure tree helpers, cycle prevention, update sentinel semantics."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from volunteerdb.db import db_session
from volunteerdb.models import Team
from volunteerdb.services import teams
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


def test_descendant_ids_includes_root_and_transitive():
    tree = _tree()
    assert teams.descendant_ids(tree, 1) == {1, 2, 3}
    assert teams.descendant_ids(tree, 2) == {2, 3}
    assert teams.descendant_ids(tree, 4) == {4}
    assert teams.descendant_ids(tree, 99) == {99}, "unknown root is just itself"


def test_team_paths_builds_parent_slash_child():
    paths = teams.team_paths(_tree())
    assert paths == {1: "Alpha", 2: "Alpha / beta", 3: "Alpha / beta / Gamma", 4: "Delta"}


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
    what PATCH refuses and the negative flows into every capacity band."""
    async with db_session() as session:
        with pytest.raises(ValueError):
            await teams.create(session, "Negative", workload_weight=Decimal("-2"))

        ok = await teams.create(session, "Fine", workload_weight=Decimal("2.5"))
        assert ok.workload_weight == Decimal("2.5")

        unweighted = await teams.create(session, "Unweighted")
        assert unweighted.workload_weight is None, "no weight at all stays legal"


async def test_update_reparent_cycle_rejected(database):
    async with db_session() as session:
        a = await teams.create(session, "A")
        b = await teams.create(session, "B", parent_team_id=a.id)
        c = await teams.create(session, "C", parent_team_id=b.id)

        with pytest.raises(CycleError):
            await teams.update(session, a.id, parent_team_id=c.id)
        with pytest.raises(CycleError):
            await teams.update(session, a.id, parent_team_id=a.id)

        # a legal reparent within the same tree still works
        moved = await teams.update(session, c.id, parent_team_id=a.id)
        assert moved.parent_team_id == a.id


async def test_update_unset_vs_explicit_none_parent(database):
    async with db_session() as session:
        parent = await teams.create(session, "Parent")
        child = await teams.create(session, "Child", parent_team_id=parent.id)

        renamed = await teams.update(session, child.id, name="Renamed")
        assert renamed.parent_team_id == parent.id, "omitted parent stays untouched"

        orphaned = await teams.update(session, child.id, parent_team_id=None)
        assert orphaned.parent_team_id is None, "explicit None detaches"


async def test_update_workload_weight_validation(database):
    async with db_session() as session:
        team = await teams.create(session, "Weighted")

        with pytest.raises(ValueError):
            await teams.update(session, team.id, workload_weight=Decimal("-1"))

        updated = await teams.update(session, team.id, workload_weight=Decimal("2.5"))
        assert updated.workload_weight == Decimal("2.5")

        cleared = await teams.update(session, team.id, workload_weight=None)
        assert cleared.workload_weight is None


async def test_two_top_level_teams_cannot_share_a_name(database):
    """The unique constraint uses NULLS NOT DISTINCT (Postgres 15+), so two
    parentless teams cannot share a name. Without it a duplicate 'Music' would
    make the importer's team-path lookup ambiguous for every future import."""
    async with db_session() as session:
        await teams.create(session, "Music")
        with pytest.raises(IntegrityError):
            await teams.create(session, "Music")

    async with db_session() as session:
        liturgy = await teams.create(session, "Liturgy")
        youth = await teams.create(session, "Youth")
        await teams.create(session, "Music", parent_team_id=liturgy.id)
        await teams.create(session, "Music", parent_team_id=youth.id)  # different parents: fine


async def test_missing_team_raises_lookup(database):
    async with db_session() as session:
        assert await teams.get(session, 424242) is None
        with pytest.raises(LookupError):
            await teams.update(session, 424242, name="X")
        with pytest.raises(LookupError):
            await teams.delete(session, 424242)
