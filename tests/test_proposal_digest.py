"""The nightly proposal digest: one email per voter per night, two notice
kinds (added to a roll / voting began), per-voter idempotency stamps.

Every elections call and the job itself take an explicit `today`, so a
proposal walks nominating -> voting -> concluded without touching the clock
(the same convention as test_elections.py).
"""

from datetime import date

import sqlalchemy as sa

from volunteerdb.db import db_session
from volunteerdb.jobs import proposal_digest
from volunteerdb.models import (
    Notification,
    NotificationStage,
    Proposal,
    ProposalVoter,
    TeamRole,
)
from volunteerdb.services import elections, mail, memberships, teams, users, volunteers

from tests import mint
from tests.fp_helpers import ok

TODAY = date(2026, 8, 10)  # nominating
D1 = date(2026, 8, 15)  # nomination deadline
VOTING_DAY = date(2026, 8, 20)  # voting
D2 = date(2026, 8, 25)  # voting deadline
AFTER = date(2026, 8, 30)  # concluded


async def _parish():
    """Liturgy: Lena leads, Cora is core, Noel is core WITHOUT an email —
    create_proposal prefills its roll with exactly those three."""
    async with db_session() as session:
        liturgy = ok(await teams.create(session, None, "Liturgy"))
        lena = ok(
            await volunteers.create(session, None, "Lena", "Leader", "lena@example.org")
        )
        cora = ok(
            await volunteers.create(session, None, "Cora", "Core", "cora@example.org")
        )
        noel = ok(await volunteers.create(session, None, "Noel", "NoEmail"))
        vera = ok(await volunteers.create(session, None, "Vera", "Volunteer"))
        ok(
            await memberships.assign(
                session, None, lena.id, liturgy.id, TeamRole.leader
            )
        )
        ok(await memberships.assign(session, None, cora.id, liturgy.id, TeamRole.core))
        ok(await memberships.assign(session, None, noel.id, liturgy.id, TeamRole.core))
        admin, _ = ok(
            await users.create(
                session, "admin@example.org", is_admin=True, invite=mint.fresh_invite()
            )
        )
        return {
            "liturgy": liturgy.id,
            "lena": lena.id,
            "cora": cora.id,
            "noel": noel.id,
            "vera": vera.id,
            "admin_u": admin.id,
        }


async def _proposal(ids, role=TeamRole.second) -> int:
    async with db_session() as session:
        proposal = ok(
            await elections.create_proposal(
                session,
                None,
                team_id=ids["liturgy"],
                role=role,
                nomination_deadline=D1,
                voting_deadline=D2,
                created_by=ids["admin_u"],
                candidates=[elections.CandidateInput(volunteer_id=ids["vera"])],
                today=TODAY,
            )
        )
        return proposal.id


async def _stamps(proposal_id: int) -> dict[int, tuple[bool, bool]]:
    """volunteer id -> (told they were added, told voting began).

    Reads models.Notification, which replaced the two columns that used to sit
    on the voter row — one row per (voter, stage) once the notice has gone out."""
    async with db_session() as session:
        voters = list(
            await session.scalars(
                sa.select(ProposalVoter).where(ProposalVoter.proposal_id == proposal_id)
            )
        )
        told = {
            (row.voter_id, row.stage)
            for row in await session.execute(
                sa.select(Notification.voter_id, Notification.stage).where(
                    Notification.voter_id.in_([v.id for v in voters])
                )
            )
        }
        return {
            v.volunteer_id: (
                (v.id, NotificationStage.roll_added) in told,
                (v.id, NotificationStage.voting_open) in told,
            )
            for v in voters
        }


def _capture(monkeypatch, ok=True):
    sent: list[tuple[str, str, str]] = []

    async def fake(to: str, subject: str, text_body: str) -> bool:
        sent.append((to, subject, text_body))
        return ok

    monkeypatch.setattr(mail, "send_email", fake)
    return sent


async def test_added_notice_once_and_voting_notice_once(database, monkeypatch, env):
    ids = await _parish()
    pid = await _proposal(ids)
    sent = _capture(monkeypatch)

    await proposal_digest.main(env, today=TODAY)
    assert {to for to, _, _ in sent} == {"lena@example.org", "cora@example.org"}, (
        "every roll member with an email — and nobody else (Noel has none)"
    )
    body = next(b for to, _, b in sent if to == "lena@example.org")
    assert "added to the voting roll" in body
    assert "August 15, 2026" in body and "August 25, 2026" in body, "both deadlines"
    stamps = await _stamps(pid)
    assert stamps[ids["lena"]] == (True, False)
    assert stamps[ids["noel"]] == (False, False), "no email, nothing stamped"

    sent.clear()
    await proposal_digest.main(env, today=TODAY)
    assert sent == [], "idempotent: the second night has nothing to say"

    await proposal_digest.main(env, today=VOTING_DAY)
    assert {to for to, _, _ in sent} == {"lena@example.org", "cora@example.org"}
    body = next(b for to, _, b in sent if to == "lena@example.org")
    assert "Voting is now open" in body and "added to the voting roll" not in body
    assert "August 25, 2026" in body, "deadlines restated"
    assert (await _stamps(pid))[ids["lena"]] == (True, True)

    sent.clear()
    await proposal_digest.main(env, today=VOTING_DAY)
    assert sent == []


async def test_added_and_voting_same_night_is_one_combined_email(
    database, monkeypatch, env
):
    ids = await _parish()
    pid = await _proposal(ids)
    sent = _capture(monkeypatch)

    await proposal_digest.main(env, today=VOTING_DAY)  # first run ever, already voting
    assert len([to for to, _, _ in sent if to == "lena@example.org"]) == 1
    body = next(b for to, _, b in sent if to == "lena@example.org")
    assert "voting is already open" in body
    assert (await _stamps(pid))[ids["lena"]] == (True, True), "both stamped at once"


async def test_two_proposals_one_email(database, monkeypatch, env):
    ids = await _parish()
    await _proposal(ids, role=TeamRole.second)
    await _proposal(ids, role=TeamRole.leader)
    sent = _capture(monkeypatch)

    await proposal_digest.main(env, today=TODAY)
    lena_mails = [b for to, _, b in sent if to == "lena@example.org"]
    assert len(lena_mails) == 1, "one email per person per night, never per proposal"
    assert "Second-in-command — Liturgy" in lena_mails[0]
    assert "Ministry leader — Liturgy" in lena_mails[0]


async def test_decided_and_concluded_proposals_stay_silent(database, monkeypatch, env):
    ids = await _parish()
    cancelled = await _proposal(ids, role=TeamRole.second)
    concluded = await _proposal(ids, role=TeamRole.leader)
    async with db_session() as session:
        # decided_at goes with the status, as elections.cancel() sets it —
        # ck_proposal_decision now says a decided proposal records when
        await session.execute(
            sa.update(Proposal)
            .where(Proposal.id == cancelled)
            .values(status="cancelled", decided_at=sa.func.now())
        )
    sent = _capture(monkeypatch)

    await proposal_digest.main(
        env, today=AFTER
    )  # `concluded` is past its voting deadline
    assert sent == []
    assert all(s == (False, False) for s in (await _stamps(concluded)).values()), (
        "awaiting-decision proposals neither notify nor stamp"
    )


async def test_failed_send_leaves_stamps_for_retry(database, monkeypatch, env):
    ids = await _parish()
    pid = await _proposal(ids)
    _capture(monkeypatch, ok=False)

    await proposal_digest.main(env, today=TODAY)
    assert all(s == (False, False) for s in (await _stamps(pid)).values())

    sent = _capture(monkeypatch, ok=True)
    await proposal_digest.main(env, today=TODAY)
    assert {to for to, _, _ in sent} == {"lena@example.org", "cora@example.org"}, (
        "the next night retries exactly the failed people"
    )


async def test_new_round_renotifies_its_fresh_roll(database, monkeypatch, env):
    ids = await _parish()
    pid = await _proposal(ids)
    sent = _capture(monkeypatch)
    await proposal_digest.main(env, today=TODAY)
    sent.clear()

    async with db_session() as session:
        fresh = ok(
            await elections.new_round(
                session,
                None,
                pid,
                created_by=ids["admin_u"],
                nomination_deadline=date(2026, 9, 5),
                voting_deadline=date(2026, 9, 15),
                today=AFTER,
                now=mint.now(),
            )
        )
        fresh_id = fresh.id

    await proposal_digest.main(env, today=date(2026, 9, 1))
    assert {to for to, _, _ in sent} == {"lena@example.org", "cora@example.org"}, (
        "cloned voter rows carry NULL stamps — a new round is new news"
    )
    assert (await _stamps(fresh_id))[ids["lena"]] == (True, False)
