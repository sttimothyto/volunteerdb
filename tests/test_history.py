"""System-versioning: updates/deletes archive old rows; as-of queries see them."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from volunteerdb.models import (
    Team,
    TeamRole,
    Volunteer,
    membership_history,
    team_history,
    volunteer_history,
)
from volunteerdb.services import memberships, teams, volunteers

from tests.conftest import before_latest_write, db_session
from tests.fp_helpers import done, ok


async def test_update_archives_old_version(database):
    async with db_session(user_id=42) as session:
        v = ok(await volunteers.create(session, None, "Old", "Name", "old@example.org"))
        vid = v.id

    async with db_session(user_id=42) as session:
        done(await volunteers.update(session, None, vid, first_name="New"))

    async with db_session() as session:
        # the instant before the rename, and the instant of it
        t_created = await before_latest_write(session, Volunteer, vid)
        t_updated = t_created + timedelta(microseconds=1)
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
        v = ok(await volunteers.create(session, None, "Gone", "Tomorrow"))
        vid = v.id

    async with db_session() as session:
        # the row's own start: it has been alive since the instant it was made
        t_alive = (await before_latest_write(session, Volunteer, vid)) + timedelta(
            microseconds=1
        )

    async with db_session(user_id=7) as session:
        ok(await volunteers.delete(session, None, vid))

    async with db_session() as session:
        assert await volunteers.get(session, vid) is None
        past = await volunteers.get(session, vid, at=t_alive)
        assert past is not None and past.first_name == "Gone"
        row = (await session.execute(sa.select(volunteer_history))).mappings().one()
        assert row["op"] == "D" and row["changed_by"] == 7


async def test_rolled_back_changes_leave_no_history(database):
    async with db_session() as session:
        v = ok(await volunteers.create(session, None, "Keep", "Me"))
        vid = v.id

    try:
        async with db_session() as session:
            done(await volunteers.update(session, None, vid, first_name="Doomed"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    async with db_session() as session:
        assert (await volunteers.get(session, vid)).first_name == "Keep"
        count = (
            await session.execute(
                sa.select(sa.func.count()).select_from(volunteer_history)
            )
        ).scalar()
        assert count == 0


# The next three tests guard the twin rebuilds (0002: volunteer_history and
# team_history; 0011: membership_history): if a twin's column order ever drifts
# from live-order + (changed_by, op), the trigger's positional INSERT breaks
# and these fail on the first UPDATE.


async def test_membership_role_changes_are_versioned(database):
    async with db_session(user_id=11) as session:
        v = ok(await volunteers.create(session, None, "Ada", "Archivist"))
        t = ok(await teams.create(session, None, "Choir"))
        vid, tid = v.id, t.id
        ok(await memberships.assign(session, None, vid, tid, TeamRole.member))

    async with db_session(user_id=11) as session:
        ok(await memberships.assign(session, None, vid, tid, TeamRole.leader))

    async with db_session() as session:
        row = (await session.execute(sa.select(membership_history))).mappings().one()
        assert row["volunteer_id"] == vid and row["team_id"] == tid
        assert row["role"] == "member"
        assert row["changed_by"] == 11 and row["op"] == "U"


async def test_custom_values_are_versioned(database):
    async with db_session(user_id=42) as session:
        v = ok(await volunteers.create(session, None, "Custom", "Carrier"))
        vid = v.id
        v.custom = {"shirt_size": "M"}

    async with db_session(user_id=42) as session:
        v = await session.get(Volunteer, vid)
        v.custom = {"shirt_size": "L"}

    async with db_session() as session:
        t_medium = await before_latest_write(session, Volunteer, vid)
        assert (await volunteers.get(session, vid)).custom == {"shirt_size": "L"}
        old = await volunteers.get(session, vid, at=t_medium)
        assert old.custom == {"shirt_size": "M"}
        row = (
            (
                await session.execute(
                    sa.select(volunteer_history).order_by(
                        volunteer_history.c.sys_period.desc()
                    )
                )
            )
            .mappings()
            .first()
        )
        assert row["custom"] == {"shirt_size": "M"}
        assert row["changed_by"] == 42 and row["op"] == "U"


async def test_workload_weight_is_versioned(database):
    """A weight change is a versioned change, like every other team edit.

    A new team weighs 0 rather than NULL: NULL was documented as "counts as 0"
    and coalesced to 0 at every read, so it was a third state with no distinct
    behaviour (models.Team.workload_weight)."""
    async with db_session(user_id=9) as session:
        t = ok(await teams.create(session, None, "Liturgy"))
        tid = t.id
        assert t.workload_weight == Decimal(0), "unweighted is 0, not NULL"

    async with db_session(user_id=9) as session:
        team = await teams.get(session, tid)
        team.workload_weight = Decimal("2.50")

    async with db_session() as session:
        t_unweighted = await before_latest_write(session, Team, tid)
        assert (await teams.get(session, tid)).workload_weight == Decimal("2.50")
        old = await teams.get(session, tid, at=t_unweighted)
        assert old.workload_weight == Decimal(0)
        row = (await session.execute(sa.select(team_history))).mappings().one()
        assert row["workload_weight"] == Decimal(0)
        assert row["changed_by"] == 9 and row["op"] == "U"


async def test_a_snapshot_row_never_enters_the_live_session(database):
    """fetch() runs an as-of query in a sibling session and expunges the rows:
    a historical version must never sit in the caller's identity map, where a
    later flush would write the past over the present."""
    async with db_session(user_id=1) as session:
        v = ok(await volunteers.create(session, None, "Then", "Now"))
        vid = v.id
    async with db_session(user_id=1) as session:
        done(await volunteers.update(session, None, vid, first_name="Since"))
    async with db_session() as session:
        before = await before_latest_write(session, Volunteer, vid)
        old = await volunteers.get(session, vid, at=before)
        assert old.first_name == "Then"
        assert old not in session, "detached: a snapshot, not a live row"
        old.first_name = "Tampered"  # a write to a snapshot must go nowhere
        await session.flush()
    async with db_session() as session:
        assert (await volunteers.get(session, vid)).first_name == "Since"
