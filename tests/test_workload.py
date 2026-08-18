"""Workload: role-multiplied workload scores, bands, config, and graph coloring."""

from decimal import Decimal

import pytest

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.permissions import load_actor
from volunteerdb.services import graph as graph_service
from volunteerdb.services import memberships, teams, users, volunteers, workload


def _config(multipliers=None, bands=None) -> workload.WorkloadConfig:
    return workload.WorkloadConfig(
        multipliers=dict(workload.DEFAULT_CONFIG.multipliers)
        if multipliers is None
        else multipliers,
        bands=list(workload.DEFAULT_CONFIG.bands) if bands is None else bands,
    )


async def test_config_default_and_roundtrip(database):
    async with db_session() as session:
        assert await workload.get_config(session) == workload.DEFAULT_CONFIG

    custom = _config(
        bands=[
            workload.Band("ok", "#4caf50", Decimal("5")),
            workload.Band("busy", "#e53935", None),
        ]
    )
    async with db_session() as session:
        await workload.set_config(session, None, custom)
    async with db_session() as session:
        loaded = await workload.get_config(session)
        assert loaded == custom
        await workload.set_config(
            session, None, workload.DEFAULT_CONFIG
        )  # upsert overwrites
    async with db_session() as session:
        assert await workload.get_config(session) == workload.DEFAULT_CONFIG


async def test_config_validation(database):
    bad_configs = [
        _config(multipliers={TeamRole.leader: Decimal("3")}),  # roles missing
        _config(
            multipliers={
                **workload.DEFAULT_CONFIG.multipliers,
                TeamRole.core: Decimal("-1"),
            }
        ),
        _config(bands=[]),
        _config(bands=[workload.Band("g", "#0f0", Decimal("4"))]),  # last band bounded
        _config(
            bands=[
                workload.Band("g", "#0f0", Decimal("8")),
                workload.Band("a", "#ff0", Decimal("4")),  # not ascending
                workload.Band("r", "#f00", None),
            ]
        ),
        _config(
            bands=[
                workload.Band("g", "#0f0", Decimal("4")),
                workload.Band("g", "#f00", None),  # duplicate label
            ]
        ),
    ]
    for bad in bad_configs:
        with pytest.raises(ValueError):
            async with db_session() as session:
                await workload.set_config(session, None, bad)


def test_band_for_boundaries():
    cfg = workload.DEFAULT_CONFIG
    assert workload.band_for(Decimal("0"), cfg).label == "green"
    assert workload.band_for(Decimal("4"), cfg).label == "green", (
        "upper bound is inclusive"
    )
    assert workload.band_for(Decimal("4.01"), cfg).label == "amber"
    assert workload.band_for(Decimal("8"), cfg).label == "amber"
    assert workload.band_for(Decimal("100"), cfg).label == "red"


async def test_scores_role_multiplied_and_null_weights(database):
    async with db_session() as session:
        liturgy = await teams.create(
            session, None, "Liturgy", workload_weight=Decimal("3")
        )
        choir = await teams.create(session, None, "Choir", workload_weight=Decimal("2"))
        social = await teams.create(
            session, None, "Social"
        )  # unweighted -> contributes 0

        busy = await volunteers.create(session, None, "Busy", "Bee")
        light = await volunteers.create(session, None, "Light", "Load")
        idle = await volunteers.create(session, None, "Idle", "Hands")

        await memberships.assign(
            session, None, busy.id, liturgy.id, TeamRole.leader
        )  # 3 × 3 = 9
        await memberships.assign(
            session, None, busy.id, choir.id, TeamRole.core
        )  # 2 × 1.5 = 3
        await memberships.assign(
            session, None, busy.id, social.id, TeamRole.leader
        )  # NULL -> 0
        await memberships.assign(
            session, None, light.id, choir.id, TeamRole.member
        )  # 2 × 1 = 2
        ids = {"busy": busy.id, "light": light.id, "idle": idle.id}

    async with db_session() as session:
        result = await workload.scores(session, list(ids.values()))
        assert result[ids["busy"]] == Decimal("12")
        assert result[ids["light"]] == Decimal("2")
        assert result[ids["idle"]] == Decimal("0"), (
            "no memberships still yields a score"
        )
        assert await workload.scores(session, []) == {}

        cfg = await workload.get_config(session)
        assert workload.band_for(result[ids["busy"]], cfg).label == "red"
        assert workload.band_for(result[ids["light"]], cfg).label == "green"


async def test_visible_scores_respects_permissions(database):
    async with db_session() as session:
        liturgy = await teams.create(
            session, None, "Liturgy", workload_weight=Decimal("2")
        )
        garden = await teams.create(
            session, None, "Garden", workload_weight=Decimal("1")
        )

        lead = await volunteers.create(session, None, "Lead", "Er")
        follower = await volunteers.create(session, None, "Fol", "Lower")
        outsider = await volunteers.create(session, None, "Out", "Sider")
        await memberships.assign(session, None, lead.id, liturgy.id, TeamRole.leader)
        await memberships.assign(
            session, None, follower.id, liturgy.id, TeamRole.member
        )
        # follower also serves elsewhere: global score must include the team
        # the leader cannot even see
        await memberships.assign(session, None, follower.id, garden.id, TeamRole.leader)
        await memberships.assign(session, None, outsider.id, garden.id, TeamRole.member)

        lead_actor = await load_actor(
            session,
            (await users.create(session, "lead@example.org", volunteer_id=lead.id))[0],
        )
        admin_actor = await load_actor(
            session,
            (await users.create(session, "admin@example.org", is_admin=True))[0],
        )

        team_sets = {
            lead.id: {liturgy.id},
            follower.id: {liturgy.id, garden.id},
            outsider.id: {garden.id},
        }
        # a leader sees their own people, including their load elsewhere
        visible = await workload.visible_scores(session, lead_actor, team_sets)
        assert set(visible) == {lead.id, follower.id}, "outsider's workload is hidden"

        visible = await workload.visible_scores(session, admin_actor, team_sets)
        assert set(visible) == {lead.id, follower.id, outsider.id}
        follower_score, follower_band = visible[follower.id]
        assert follower_score == Decimal("5"), (
            "2×1 (member of Liturgy) + 1×3 (leads Garden)"
        )
        assert follower_band.label == "amber"


async def test_graph_colors_only_permitted_nodes(database):
    async with db_session() as session:
        liturgy = await teams.create(
            session, None, "Liturgy", workload_weight=Decimal("2")
        )
        garden = await teams.create(
            session, None, "Garden", workload_weight=Decimal("1")
        )

        lead = await volunteers.create(session, None, "Lead", "Er")
        follower = await volunteers.create(session, None, "Fol", "Lower")
        watcher = await volunteers.create(session, None, "Core", "Watcher")
        await memberships.assign(session, None, lead.id, liturgy.id, TeamRole.leader)
        await memberships.assign(
            session, None, follower.id, liturgy.id, TeamRole.member
        )
        await memberships.assign(session, None, follower.id, garden.id, TeamRole.leader)
        await memberships.assign(session, None, watcher.id, liturgy.id, TeamRole.core)

        lead_actor = await load_actor(
            session,
            (await users.create(session, "lead@example.org", volunteer_id=lead.id))[0],
        )
        core_actor = await load_actor(
            session,
            (await users.create(session, "core@example.org", volunteer_id=watcher.id))[
                0
            ],
        )
        admin_actor = await load_actor(
            session,
            (await users.create(session, "admin@example.org", is_admin=True))[0],
        )

        def volunteer_nodes(elements):
            return {
                n["data"]["volunteer_id"]: n["data"]
                for n in elements["nodes"]
                if n["data"]["type"] == "volunteer"
            }

        # the leader sees bands on their people, as does an admin; follower's
        # band reflects the Garden team too, even when the graph is focused on
        # Liturgy only
        for actor in (lead_actor, admin_actor):
            graph = volunteer_nodes(
                await graph_service.elements(session, actor, team_id=liturgy.id)
            )
            assert graph[follower.id]["band"] == "amber", (
                "2×1 + 1×3 = 5, includes unseen Garden"
            )
            assert graph[follower.id]["color"] == "#ffb300"
            assert graph[lead.id]["band"] == "amber", "leader of weight-2 team: 2×3 = 6"

        # a core member sees the same people but never their workload
        graph = volunteer_nodes(await graph_service.elements(session, core_actor))
        assert follower.id in graph and lead.id in graph
        assert all("color" not in d and "band" not in d for d in graph.values())
