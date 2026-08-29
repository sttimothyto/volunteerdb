"""Nightly proposal digest (in-app scheduler, VDB_PROPOSAL_DIGEST_AT): tell
voters what needs their input.

Exactly two notices exist, batched into ONE email per person per night no
matter how many proposals it covers: (a) you were added to a proposal's
voting roll, (b) a proposal you sit on moved from nominating to voting. Both
carry the nomination and voting deadlines. The per-voter stamps
(rows in `notification`, keyed by (voter, stage)) make each notice
one-shot and per-person idempotent: a failed send leaves its stamps NULL and
retries the next night; a crash mid-run re-sends at most the unstamped
people. Phases derive from dates in the parish's day (elections.phase_of),
never the container's UTC clock.

read -> plan -> execute, like event_reminders: `plan` is pure and tested
without a database.

Usage: python -m volunteerdb.jobs.proposal_digest [--today YYYY-MM-DD]
(--today exists for manual runs and tests; defaults to the parish's today)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import env as env_mod
from ..db import transaction
from ..env import Env
from ..log import init_logging
from ..models import (
    ROLE_LABELS,
    Notification,
    NotificationStage,
    Proposal,
    ProposalStatus,
    ProposalVoter,
    Volunteer,
)
from ..services import elections, mail
from ..services import teams as team_service
from . import JobReport, job_lock


@dataclass(frozen=True)
class VoterRow:
    """One voter on an open proposal who may still be owed a notice."""

    voter_id: int
    volunteer_id: int
    email: str
    team_id: int
    role: str
    nomination_deadline: date
    voting_deadline: date


type Stamp = tuple[int, NotificationStage]  # (voter id, stage)


@dataclass(frozen=True)
class Digest:
    email: str
    items: tuple[mail.DigestItem, ...]
    stamps: tuple[Stamp, ...]


def _unsent(stage: NotificationStage):
    """ "this voter has had no `stage` notice yet", as a NOT EXISTS."""
    return ~sa.exists().where(
        Notification.voter_id == ProposalVoter.id,
        Notification.stage == stage,
    )


async def read(
    session: AsyncSession,
) -> tuple[list[VoterRow], dict[int, str], frozenset[Stamp]]:
    rows = (
        await session.execute(
            sa.select(ProposalVoter, Proposal, Volunteer)
            .join(Proposal, Proposal.id == ProposalVoter.proposal_id)
            .join(Volunteer, Volunteer.id == ProposalVoter.volunteer_id)
            .where(
                Proposal.status == ProposalStatus.open,
                Volunteer.email.is_not(None),
                sa.or_(
                    _unsent(NotificationStage.roll_added),
                    _unsent(NotificationStage.voting_open),
                ),
            )
            .order_by(Proposal.id)
        )
    ).all()
    paths = (await team_service.tree(session)).paths
    already: set[Stamp] = set()
    if rows:
        already = {
            (row.voter_id, row.stage)
            for row in await session.execute(
                sa.select(Notification.voter_id, Notification.stage).where(
                    Notification.voter_id.in_([v.id for v, _p, _vol in rows])
                )
            )
        }
    plain = [
        VoterRow(
            voter_id=voter.id,
            volunteer_id=volunteer.id,
            email=volunteer.email,
            team_id=proposal.team_id,
            role=proposal.role,
            nomination_deadline=proposal.nomination_deadline,
            voting_deadline=proposal.voting_deadline,
        )
        for voter, proposal, volunteer in rows
    ]
    return plain, paths, frozenset(already)


def plan(
    rows: Sequence[VoterRow],
    paths: dict[int, str],
    already: frozenset[Stamp],
    *,
    today: date,
) -> list[Digest]:
    """Who hears what tonight, and which stamps that settles. Pure."""
    per_person: dict[int, tuple[str, list[mail.DigestItem], list[Stamp]]] = {}
    for row in rows:
        phase = elections.phase_of_dates(
            row.nomination_deadline, row.voting_deadline, today
        )
        pending_added = (
            row.voter_id,
            NotificationStage.roll_added,
        ) not in already and (
            phase
            in (elections.ProposalPhase.nominating, elections.ProposalPhase.voting)
        )
        pending_voting = (
            row.voter_id,
            NotificationStage.voting_open,
        ) not in already and phase is elections.ProposalPhase.voting
        if not pending_added and not pending_voting:
            continue  # concluded (awaiting decision), or already notified
        kind = (
            "both"
            if pending_added and pending_voting
            else ("added" if pending_added else "voting")
        )
        seat = (
            f"{ROLE_LABELS[row.role]} — {paths.get(row.team_id, f'team {row.team_id}')}"
        )
        email, items, stamps = per_person.setdefault(
            row.volunteer_id, (row.email, [], [])
        )
        items.append(
            mail.DigestItem(
                kind=kind,
                seat=seat,
                nomination_deadline=row.nomination_deadline,
                voting_deadline=row.voting_deadline,
            )
        )
        if pending_added:
            stamps.append((row.voter_id, NotificationStage.roll_added))
        if pending_voting:
            stamps.append((row.voter_id, NotificationStage.voting_open))
    return [
        Digest(email, tuple(items), tuple(stamps))
        for email, items, stamps in per_person.values()
    ]


async def execute(digests: Sequence[Digest], env: Env) -> JobReport:
    sent = failed = 0
    for digest in digests:
        subject, body = mail.proposal_digest_email(list(digest.items))
        if not await env.mailer.send(digest.email, subject, body):
            failed += 1
            print(f"FAILED digest to {digest.email}", file=sys.stderr)
            continue  # nothing recorded — retried the next night
        async with transaction(env, None) as session:
            await session.execute(
                pg_insert(Notification)
                .values([{"voter_id": i, "stage": s} for i, s in digest.stamps])
                .on_conflict_do_nothing(constraint="uq_notification_voter")
            )
        sent += 1
    return JobReport(sent=sent, failed=failed)


async def main(env: Env, today: date | None = None) -> int:
    init_logging()
    if today is None:
        today = env.today()
    async with transaction(env, None) as session:
        rows, paths, already = await read(session)
    report = await execute(plan(rows, paths, already, today=today), env)
    print(f"digest: {report.sent} person(s) emailed, {report.failed} failure(s)")
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="pretend today is this date (parish day); for manual runs and tests",
    )
    args = parser.parse_args(argv)

    async def locked() -> int:
        env = env_mod.build()
        async with job_lock(env, "proposal_digest") as acquired:
            if not acquired:
                print("skipped: another proposal_digest run holds the job lock")
                return 0
            return await main(env, today=args.today)

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
