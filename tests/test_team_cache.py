"""The team tree is read once per session, and the memo can never go stale.

Nothing else in the suite asserts a query count. This does, because the win is
invisible to behavioural tests: every assertion about paths and subtrees passed
just as well when /events read the whole team table four times over. The count
here is the only thing that fails when a later change reintroduces a bare
per-caller read.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from volunteerdb.db import db_session, sessionmaker
from volunteerdb.models import Team, TeamRole
from volunteerdb.permissions import load_actor
from volunteerdb.services import (
    events,
    memberships,
    pages,
    teams,
    users,
    volunteers,
)

from .conftest import TEAM_TREE_SQL, count_sql


async def _now() -> datetime:
    await asyncio.sleep(0.02)  # clock_timestamp() granularity margin, as test_history
    now = datetime.now(UTC)
    await asyncio.sleep(0.02)
    return now


@pytest.fixture
async def parish(database):
    """Liturgy > Music, plus Hospitality. Lena leads both and may create events.

    One scheduled event, because list_events() returns before it ever looks at
    the team tree when there are none — an empty calendar hides one of the four
    reads this file is here to count.
    """
    async with db_session() as session:
        liturgy = await teams.create(session, None, "Liturgy")
        music = await teams.create(session, None, "Music", parent_team_id=liturgy.id)
        hospitality = await teams.create(session, None, "Hospitality")
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        await memberships.assign(session, None, lena.id, liturgy.id, TeamRole.leader)
        await memberships.assign(
            session, None, lena.id, hospitality.id, TeamRole.leader
        )
        lena_u, _ = await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            password="test-pass-phrase",
        )
        start = datetime.now(UTC) + timedelta(days=7)
        await events.create_event(
            session,
            None,
            team_id=liturgy.id,
            title="Sunday setup",
            starts_at=start,
            ends_at=start + timedelta(hours=2),
            created_by=None,
        )
        return {
            "liturgy": liturgy.id,
            "music": music.id,
            "hospitality": hospitality.id,
            "lena_u": lena_u.id,
        }


async def test_the_events_prologue_reads_the_team_table_once(parish):
    """The worst stack measured before the memo: permissions.load_actor, then
    claimable_subs, then list_events, then the page's own read — four full reads
    of the team table inside one session.

    Mirrors the data block of ui/events_page.py — keep in sync.
    """
    async with db_session() as session:
        user = await users.get(session, parish["lena_u"])
        with count_sql(TEAM_TREE_SQL) as seen:
            actor = await load_actor(session, user)
            await events.claimable_subs(session, actor)
            await events.list_events(session, actor)
            await teams.tree(session)
        assert len(seen) == 1, f"the team tree was read {len(seen)} times, not once"


async def test_each_front_door_gets_its_own_memo(parish):
    """Sessions do not share it: the memo dies with the unit of work."""
    async with db_session() as session:
        with count_sql(TEAM_TREE_SQL) as seen:
            await teams.tree(session)
            await teams.tree(session)
        assert len(seen) == 1

    async with db_session() as session:
        with count_sql(TEAM_TREE_SQL) as seen:
            await teams.tree(session)
        assert len(seen) == 1, "a new session must not inherit the previous memo"


async def test_a_created_team_is_visible_to_the_next_read(parish):
    async with db_session() as session:
        assert "Servers" not in (await teams.tree(session)).paths.values()
        made = await teams.create(session, None, "Servers")
        after = await teams.tree(session)
        assert after.paths[made.id] == "Servers"


async def test_a_rename_changes_the_paths_of_the_children_too(parish):
    async with db_session() as session:
        before = await teams.tree(session)
        assert before.paths[parish["music"]] == "Liturgy / Music"
        await teams.update(session, None, parish["liturgy"], name="Worship")
        after = await teams.tree(session)
        assert after.paths[parish["music"]] == "Worship / Music"


async def test_a_reparent_changes_the_shape(parish):
    async with db_session() as session:
        before = await teams.tree(session)
        assert before.descendants(parish["hospitality"]) == {parish["hospitality"]}
        await teams.update(
            session, None, parish["music"], parent_team_id=parish["hospitality"]
        )
        after = await teams.tree(session)
        assert after.descendants(parish["hospitality"]) == {
            parish["hospitality"],
            parish["music"],
        }


async def test_a_deleted_team_leaves_the_memo(parish):
    async with db_session() as session:
        assert parish["music"] in (await teams.tree(session)).paths
        await teams.delete(session, None, parish["music"])
        assert parish["music"] not in (await teams.tree(session)).paths


async def test_a_home_doc_url_set_outside_the_teams_service_invalidates(parish):
    """services/pages.py writes team.home_doc_url and never touches
    services/teams.py. It is the mutator an explicit invalidate() would miss, and
    the reason invalidation is a session listener instead."""
    url = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"
    async with db_session() as session:
        before = await teams.tree(session)
        assert all(t.home_doc_url is None for t in before.teams)
        await pages.set_home_doc_url(session, None, parish["liturgy"], url)
        after = await teams.tree(session)
        assert {t.id: t.home_doc_url for t in after.teams}[parish["liturgy"]] == url


async def test_a_rolled_back_team_does_not_survive_in_the_memo(parish):
    """db_session() owns its transaction and rolling back inside its block closes
    it, so this drives a bare session to reach the after_rollback path."""
    async with sessionmaker()() as session:
        made = await teams.create(session, None, "Provisional")
        assert made.id in (await teams.tree(session)).paths
        await session.rollback()
        assert "Provisional" not in (await teams.tree(session)).paths.values()


async def test_a_snapshot_is_never_served_for_a_live_read(parish):
    async with db_session() as session:
        await teams.update(session, None, parish["liturgy"], name="Worship")
    t_renamed = await _now()

    async with db_session() as session:
        await teams.update(session, None, parish["liturgy"], name="Divine Worship")

    async with db_session() as session:
        # both keys live in one session; neither may answer for the other
        snapshot = await teams.tree(session, at=t_renamed)
        live = await teams.tree(session)
        assert snapshot.paths[parish["liturgy"]] == "Worship"
        assert live.paths[parish["liturgy"]] == "Divine Worship"
        assert snapshot is not live

        with count_sql(TEAM_TREE_SQL) as seen:
            await teams.tree(session, at=t_renamed)
            await teams.tree(session)
        assert not seen, "both keys were already memoized"


async def test_the_cycle_check_still_bites_with_a_warm_memo(parish):
    async with db_session() as session:
        await teams.tree(session)  # warm it before the mutation
        with pytest.raises(teams.CycleError):
            await teams.update(
                session, None, parish["liturgy"], parent_team_id=parish["music"]
            )


async def test_every_team_mutator_flushes_before_it_returns(parish):
    """The memo is dropped by an after_flush listener, so a Team change that is
    still pending — set on the instance, not yet flushed — would be invisible to
    the next read. Nothing in the app does that today because every mutator
    flushes before returning, and this is the test that keeps it true.
    """
    url = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"

    def pending(session) -> list:
        return [o for o in (*session.new, *session.dirty) if isinstance(o, Team)]

    async with db_session() as session:
        made = await teams.create(session, None, "Servers")
        assert not pending(session), "teams.create left a Team unflushed"

        await teams.update(session, None, made.id, name="Altar Servers")
        assert not pending(session), "teams.update left a Team unflushed"

        await pages.set_home_doc_url(session, None, made.id, url)
        assert not pending(session), "pages.set_home_doc_url left a Team unflushed"

        await teams.delete(session, None, made.id)
        assert not pending(session), "teams.delete left a Team unflushed"
