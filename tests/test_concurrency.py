"""Two things at once. Each of the app's race defences, exercised by two
tasks that really overlap on separate connections, so the defence is what
decides the outcome and not the order the tests happened to run in."""

import asyncio
from datetime import timedelta

import sqlalchemy as sa

from volunteerdb import errors
from volunteerdb.fp import Err, Ok
from volunteerdb.jobs import job_lock
from volunteerdb.models import MailQuota, TeamRole
from volunteerdb.services import events as event_service
from volunteerdb.services import memberships, teams, volunteers

from tests import mint
from tests.conftest import db_session
from tests.fp_helpers import ok


async def _team_with_members(n: int) -> tuple[int, list[int]]:
    async with db_session() as session:
        team = ok(await teams.create(session, None, "Liturgy"))
        vids = []
        for i in range(n):
            v = ok(
                await volunteers.create(
                    session, None, f"Vol{i}", "Server", f"vol{i}@example.org"
                )
            )
            ok(await memberships.assign(session, None, v.id, team.id, TeamRole.member))
            vids.append(v.id)
        return team.id, vids


async def _event_with_one_seat(team_id: int) -> int:
    """An event whose only slot holds one person: the counted capacity that
    has no unique index behind it -- the case the FOR UPDATE exists for."""
    async with db_session() as session:
        start = mint.now() + timedelta(days=7)
        created = ok(
            await event_service.create_event(
                session,
                None,
                team_id=team_id,
                title="Mass",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                slots=[event_service.SlotInput("Lector", 1)],
                created_by=None,
                tz=mint.tz(),
                series_id=mint.uuid(),
            )
        )
        view = ok(await event_service.detail(session, None, created[0].id))
        return view.slots[0].slot.id


async def test_two_sign_ups_for_the_last_seat_serialize_on_the_slot_lock(database):
    """Both count a free seat; the row lock makes the second count again
    after the first commits, so exactly one gets it and the other is told
    the slot is full -- not two assignments and an overbooked slot."""
    team_id, (a, b) = await _team_with_members(2)
    slot_id = await _event_with_one_seat(team_id)
    started = asyncio.Barrier(2)

    async def sign_up(volunteer_id: int):
        async with db_session() as session:
            await started.wait()  # both hold a connection before either locks
            return await event_service.sign_up(
                session,
                None,
                slot_id=slot_id,
                volunteer_id=volunteer_id,
                now=mint.now(),
            )

    first, second = await asyncio.gather(sign_up(a), sign_up(b))
    outcomes = sorted((first, second), key=lambda r: isinstance(r, Err))
    assert isinstance(outcomes[0], Ok) and isinstance(outcomes[1], Err)
    assert isinstance(outcomes[1].error, errors.Invalid)
    assert "full" in outcomes[1].error.message

    async with db_session() as session:
        count = await session.scalar(
            sa.select(sa.func.count()).select_from(sa.text("event_assignment"))
        )
    assert count == 1, "the capacity held under contention"


async def test_the_job_lock_admits_one_holder_at_a_time(env):
    """The advisory lock is per cluster and per job name: a second run of the
    same job while one is in flight is told to skip; a different job is not."""
    holding = asyncio.Event()
    tried = asyncio.Barrier(3)  # the two contenders and the release

    async def first_run() -> bool:
        async with job_lock(env, "roster_sync") as acquired:
            holding.set()
            await tried.wait()  # held until both contenders have asked
            return acquired

    async def contend(name: str) -> bool:
        await holding.wait()
        async with job_lock(env, name) as acquired:
            await tried.wait()
            return acquired

    first, second, other = await asyncio.gather(
        first_run(), contend("roster_sync"), contend("calendar_sync")
    )
    assert (first, second, other) == (True, False, True)

    async with job_lock(env, "roster_sync") as acquired:
        assert acquired, "released with the connection, not leaked"


async def test_concurrent_mail_counts_land_on_one_row(env):
    """Every message that leaves counts itself in; twenty at once must not
    lose any to a read-modify-write race (the ledger is an upsert)."""
    day = mint.today()
    await asyncio.gather(*(env.quota.record(env.sessions, day) for _ in range(20)))
    async with db_session() as session:
        rows = (await session.execute(sa.select(MailQuota.day, MailQuota.sent))).all()
    assert dict(rows) == {day: 20}


async def test_a_double_submitted_nomination_is_one_201_and_one_409(
    client, seeded, token_leader
):
    """The same candidate put forward twice at once: the unique constraint
    is the arbiter, and the API turns the loser into a 409 rather than a 500."""
    async with db_session() as session:
        walter = ok(await volunteers.create(session, None, "Walter", "Willing"))
        walter_id = walter.id
    r = await client.post(
        "/api/elections/proposals",
        json={
            "team_id": seeded["team_id"],
            "role": "second",
            "nomination_deadline": (mint.today() + timedelta(days=3)).isoformat(),
            "voting_deadline": (mint.today() + timedelta(days=10)).isoformat(),
            "candidates": [{"volunteer_id": seeded["volunteer_id"]}],
        },
        headers=token_leader,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    nominate = client.post(
        f"/api/elections/proposals/{pid}/candidates",
        json={"volunteer_id": walter_id},
        headers=token_leader,
    )
    again = client.post(
        f"/api/elections/proposals/{pid}/candidates",
        json={"volunteer_id": walter_id},
        headers=token_leader,
    )
    responses = await asyncio.gather(nominate, again)
    assert sorted(r.status_code for r in responses) == [201, 409], [
        (r.status_code, r.text) for r in responses
    ]
    detail = await client.get(f"/api/elections/proposals/{pid}", headers=token_leader)
    assert [c["volunteer_id"] for c in detail.json()["candidates"]].count(
        walter_id
    ) == 1
