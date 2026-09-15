"""Workload: role-multiplied workload scores, bands, config, and graph coloring."""

from decimal import Decimal

from volunteerdb import errors
from volunteerdb.actors import load_actor
from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import graph as graph_service
from volunteerdb.services import memberships, teams, users, volunteers, workload

from tests import conftest, mint
from tests.conftest import count_sql, db_session
from tests.fp_helpers import ok, refused

# the config row read_config looks up; the memo below is counted against it
WORKLOAD_CONFIG_SQL = r"FROM app_setting"


def _config(multipliers=None, bands=None) -> workload.WorkloadConfig:
    return workload.WorkloadConfig(
        multipliers=dict(workload.DEFAULT_CONFIG.multipliers)
        if multipliers is None
        else multipliers,
        bands=list(workload.DEFAULT_CONFIG.bands) if bands is None else bands,
    )


async def test_config_default_and_roundtrip(database):
    async with db_session() as session:
        assert await workload.read_config(session) == workload.DEFAULT_CONFIG

    custom = _config(
        bands=[
            workload.Band("ok", "#4caf50", Decimal("5")),
            workload.Band("busy", "#c62828", None),
        ]
    )
    async with db_session() as session:
        ok(await workload.set_config(session, SYSTEM, custom, now=mint.now()))
    async with db_session() as session:
        loaded = await workload.read_config(session)
        assert loaded == custom
        ok(
            await workload.set_config(
                session, SYSTEM, workload.DEFAULT_CONFIG, now=mint.now()
            )
        )  # upsert overwrites
    async with db_session() as session:
        assert await workload.read_config(session) == workload.DEFAULT_CONFIG


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
        async with db_session() as session:
            refused(
                await workload.set_config(session, SYSTEM, bad, now=mint.now()),
                errors.Invalid,
            )


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
        liturgy = ok(
            await teams.create(session, SYSTEM, "Liturgy", workload_weight=Decimal("3"))
        )
        choir = ok(
            await teams.create(session, SYSTEM, "Choir", workload_weight=Decimal("2"))
        )
        social = ok(
            await teams.create(session, SYSTEM, "Social")
        )  # unweighted -> contributes 0

        busy = ok(await volunteers.create(session, SYSTEM, "Busy", "Bee"))
        light = ok(await volunteers.create(session, SYSTEM, "Light", "Load"))
        idle = ok(await volunteers.create(session, SYSTEM, "Idle", "Hands"))

        ok(
            await memberships.assign(
                session, SYSTEM, busy.id, liturgy.id, TeamRole.leader
            )
        )  # 3 × 3 = 9
        ok(
            await memberships.assign(session, SYSTEM, busy.id, choir.id, TeamRole.core)
        )  # 2 × 1.5 = 3
        ok(
            await memberships.assign(
                session, SYSTEM, busy.id, social.id, TeamRole.leader
            )
        )  # NULL -> 0
        ok(
            await memberships.assign(
                session, SYSTEM, light.id, choir.id, TeamRole.member
            )
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

        cfg = await workload.read_config(session)
        assert workload.band_for(result[ids["busy"]], cfg).label == "red"
        assert workload.band_for(result[ids["light"]], cfg).label == "green"


async def test_visible_scores_respects_permissions(database):
    async with db_session() as session:
        liturgy = ok(
            await teams.create(session, SYSTEM, "Liturgy", workload_weight=Decimal("2"))
        )
        garden = ok(
            await teams.create(session, SYSTEM, "Garden", workload_weight=Decimal("1"))
        )

        lead = ok(await volunteers.create(session, SYSTEM, "Lead", "Er"))
        follower = ok(await volunteers.create(session, SYSTEM, "Fol", "Lower"))
        outsider = ok(await volunteers.create(session, SYSTEM, "Out", "Sider"))
        ok(
            await memberships.assign(
                session, SYSTEM, lead.id, liturgy.id, TeamRole.leader
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, follower.id, liturgy.id, TeamRole.member
            )
        )
        # follower also serves elsewhere: global score must include the team
        # the leader cannot even see
        ok(
            await memberships.assign(
                session, SYSTEM, follower.id, garden.id, TeamRole.leader
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, outsider.id, garden.id, TeamRole.member
            )
        )

        lead_actor = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "lead@example.org",
                        volunteer_id=lead.id,
                        invite=mint.fresh_invite(),
                        actor=SYSTEM,
                    )
                )
            )[0],
        )
        admin_actor = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "admin@example.org",
                        is_admin=True,
                        invite=mint.fresh_invite(),
                        actor=SYSTEM,
                    )
                )
            )[0],
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
        liturgy = ok(
            await teams.create(session, SYSTEM, "Liturgy", workload_weight=Decimal("2"))
        )
        garden = ok(
            await teams.create(session, SYSTEM, "Garden", workload_weight=Decimal("1"))
        )

        lead = ok(await volunteers.create(session, SYSTEM, "Lead", "Er"))
        follower = ok(await volunteers.create(session, SYSTEM, "Fol", "Lower"))
        watcher = ok(await volunteers.create(session, SYSTEM, "Core", "Watcher"))
        ok(
            await memberships.assign(
                session, SYSTEM, lead.id, liturgy.id, TeamRole.leader
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, follower.id, liturgy.id, TeamRole.member
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, follower.id, garden.id, TeamRole.leader
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, watcher.id, liturgy.id, TeamRole.core
            )
        )

        lead_actor = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "lead@example.org",
                        volunteer_id=lead.id,
                        invite=mint.fresh_invite(),
                        actor=SYSTEM,
                    )
                )
            )[0],
        )
        core_actor = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "core@example.org",
                        volunteer_id=watcher.id,
                        invite=mint.fresh_invite(),
                        actor=SYSTEM,
                    )
                )
            )[0],
        )
        admin_actor = await load_actor(
            session,
            (
                ok(
                    await users.create(
                        session,
                        "admin@example.org",
                        is_admin=True,
                        invite=mint.fresh_invite(),
                        actor=SYSTEM,
                    )
                )
            )[0],
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


async def test_the_config_is_read_once_per_session(database):
    """Every page that paints a band asks for the config, and none of them
    knows whether another already has: the legend asks, the table asks, and
    visible_scores asks again on its way to band_for. The dashboard used to pay
    four round trips for one answer — and the *miss* is the ordinary case,
    since no app_setting row exists until an admin changes something and
    session.get caches a hit but not a miss.

    Counted rather than asserted behaviourally, for the reason
    tests/test_team_cache.py gives: the numbers come out identical either way.
    """
    async with db_session() as session:
        busy = ok(await volunteers.create(session, SYSTEM, "Busy", "Bee"))
        team = ok(
            await teams.create(session, SYSTEM, "Liturgy", workload_weight=Decimal("2"))
        )
        ok(await memberships.assign(session, SYSTEM, busy.id, team.id, TeamRole.leader))

    async with db_session() as session:
        # mirrors the data block of ui/volunteers_page.py
        with count_sql(WORKLOAD_CONFIG_SQL) as seen:
            await workload.read_config(session)
            await workload.visible_scores(session, SYSTEM, {busy.id: {team.id}})
        assert len(seen) == 1, f"the config was read {len(seen)} times, not once"

    async with db_session() as session:
        with count_sql(WORKLOAD_CONFIG_SQL) as seen:
            await workload.read_config(session)
        assert len(seen) == 1, "a new session must not inherit the previous memo"


async def test_a_session_that_writes_the_config_stops_memoising_it(database):
    """set_config goes in as a Core upsert, so no after_flush listener could
    drop a memo the way team_cache.py's does. The memo is disabled for the rest
    of the session instead — which is also what keeps a rolled-back write from
    leaving a stale answer behind."""
    custom = _config(
        bands=[
            workload.Band("ok", "#4caf50", Decimal("5")),
            workload.Band("busy", "#c62828", None),
        ]
    )
    async with db_session() as session:
        assert await workload.read_config(session) == workload.DEFAULT_CONFIG
        ok(await workload.set_config(session, SYSTEM, custom, now=mint.now()))
        with count_sql(WORKLOAD_CONFIG_SQL) as seen:
            assert await workload.read_config(session) == custom
            assert await workload.read_config(session) == custom
        assert len(seen) == 2, "a written config is re-read, never memoised"

    # db_session() owns its transaction and rolling back inside its block closes
    # it, so this drives a bare session, as tests/test_team_cache.py does
    abandoned = _config(bands=[workload.Band("all", "#c62828", None)])
    async with conftest.SESSIONS() as session:
        ok(await workload.set_config(session, SYSTEM, abandoned, now=mint.now()))
        assert await workload.read_config(session) == abandoned
        await session.rollback()
        assert await workload.read_config(session) == custom, (
            "the rolled-back write must not survive in a memo"
        )
