"""The unit of work's two contracts that the Result migration leans on.

Today a service that refuses RAISES, and the exception unwinding
``session.begin()`` is what rolls the transaction back. A service that returns
``Err`` instead unwinds nothing, so the edge has to abort explicitly -- and
``await session.rollback()`` inside the ``begin()`` block is that abort:
SQLAlchemy's context manager commits only while its transaction is still
active (TransactionalContext.__exit__), so after a rollback the block exit is
a no-op and nothing that was flushed survives. A constraint violation, on the
other hand, still propagates as an exception (the transaction is dead) and
the boundary maps it to Conflict.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from volunteerdb.models import Membership, Team, TeamRole, Volunteer

from tests.conftest import db_session


async def _team_count() -> int:
    async with db_session() as session:
        return await session.scalar(sa.select(sa.func.count()).select_from(Team))


async def test_rollback_inside_begin_skips_the_commit():
    async with db_session() as session:
        session.add(Team(name="Ghost"))
        await session.flush()
        assert await session.scalar(sa.select(sa.func.count()).select_from(Team)) == 1
        await session.rollback()
    assert await _team_count() == 0


async def test_a_clean_exit_still_commits():
    async with db_session() as session:
        session.add(Team(name="Real"))
        await session.flush()
    assert await _team_count() == 1


async def test_integrity_error_propagates_and_commits_nothing():
    with pytest.raises(IntegrityError):
        async with db_session() as session:
            team = Team(name="Liturgy")
            person = Volunteer(first_name="Ann", last_name="Lee")
            session.add_all([team, person])
            await session.flush()
            session.add(
                Membership(
                    volunteer_id=person.id, team_id=team.id, role=TeamRole.member
                )
            )
            await session.flush()
            session.add(
                Membership(volunteer_id=person.id, team_id=team.id, role=TeamRole.core)
            )
            await session.flush()
    assert await _team_count() == 0
