"""System-versioning: updates/deletes archive old rows; as-of queries see them."""

import asyncio
from datetime import UTC, datetime

import sqlalchemy as sa

from volunteerdb.db import db_session
from volunteerdb.models import Volunteer, volunteer_history
from volunteerdb.services import volunteers


async def _now() -> datetime:
    await asyncio.sleep(0.02)  # clock_timestamp() granularity margin
    now = datetime.now(UTC)
    await asyncio.sleep(0.02)
    return now


async def test_update_archives_old_version(database):
    async with db_session(user_id=42) as session:
        v = await volunteers.create(session, "Old", "Name", "old@example.org")
        vid = v.id
    t_created = await _now()

    async with db_session(user_id=42) as session:
        await volunteers.update(session, vid, first_name="New")
    t_updated = await _now()

    async with db_session() as session:
        # live row has the new name
        live = await volunteers.get(session, vid)
        assert live.first_name == "New"
        # as of t_created, the old name is visible
        old = await volunteers.get(session, vid, at=t_created)
        assert old.first_name == "Old"
        # as of t_updated, the new one
        new = await volunteers.get(session, vid, at=t_updated)
        assert new.first_name == "New"
        # history row carries changed_by and op
        row = (await session.execute(sa.select(volunteer_history))).mappings().one()
        assert row["changed_by"] == 42
        assert row["op"] == "U"
        assert row["first_name"] == "Old"


async def test_delete_is_visible_in_the_past(database):
    async with db_session(user_id=7) as session:
        v = await volunteers.create(session, "Gone", "Tomorrow")
        vid = v.id
    t_alive = await _now()

    async with db_session(user_id=7) as session:
        await volunteers.delete(session, vid)

    async with db_session() as session:
        assert await volunteers.get(session, vid) is None
        past = await volunteers.get(session, vid, at=t_alive)
        assert past is not None and past.first_name == "Gone"
        row = (await session.execute(sa.select(volunteer_history))).mappings().one()
        assert row["op"] == "D" and row["changed_by"] == 7


async def test_rolled_back_changes_leave_no_history(database):
    async with db_session() as session:
        v = await volunteers.create(session, "Keep", "Me")
        vid = v.id

    try:
        async with db_session() as session:
            await volunteers.update(session, vid, first_name="Doomed")
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    async with db_session() as session:
        assert (await volunteers.get(session, vid)).first_name == "Keep"
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(volunteer_history))
        ).scalar()
        assert count == 0
