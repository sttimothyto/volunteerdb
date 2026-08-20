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

Usage: python -m volunteerdb.jobs.proposal_digest [--today YYYY-MM-DD]
(--today exists for manual runs and tests; defaults to the parish's today)
"""

import argparse
import asyncio
import sys
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import db_session
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
from . import job_lock


def _unsent(stage: NotificationStage):
    """ "this voter has had no `stage` notice yet", as a NOT EXISTS."""
    return ~sa.exists().where(
        Notification.voter_id == ProposalVoter.id,
        Notification.stage == stage,
    )


async def main(today: date | None = None) -> int:
    init_logging()
    if today is None:
        today = elections.local_today()

    async with db_session() as session:
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
        already = (
            {
                (row.voter_id, row.stage)
                for row in await session.execute(
                    sa.select(Notification.voter_id, Notification.stage).where(
                        Notification.voter_id.in_([v.id for v, _p, _vol in rows])
                    )
                )
            }
            if rows
            else set()
        )

    # volunteer id -> (email, digest items, voter ids to stamp per notice)
    per_person: dict[int, tuple[str, list[mail.DigestItem], list[int], list[int]]] = {}
    for voter, proposal, volunteer in rows:
        phase = elections.phase_of(proposal, today)
        pending_added = (
            voter.id,
            NotificationStage.roll_added,
        ) not in already and phase in (
            elections.ProposalPhase.nominating,
            elections.ProposalPhase.voting,
        )
        pending_voting = (
            voter.id,
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
            f"{ROLE_LABELS[proposal.role]} — "
            f"{paths.get(proposal.team_id, f'team {proposal.team_id}')}"
        )
        entry = per_person.setdefault(volunteer.id, (volunteer.email, [], [], []))
        entry[1].append(
            mail.DigestItem(
                kind=kind,
                seat=seat,
                nomination_deadline=proposal.nomination_deadline,
                voting_deadline=proposal.voting_deadline,
            )
        )
        if pending_added:
            entry[2].append(voter.id)
        if pending_voting:
            entry[3].append(voter.id)

    sent = failed = 0
    for email, items, added_ids, voting_ids in per_person.values():
        if not await mail.send_email(email, *mail.proposal_digest_email(items)):
            failed += 1
            print(f"FAILED digest to {email}", file=sys.stderr)
            continue  # nothing recorded — retried the next night
        async with db_session() as session:
            for ids, stage in (
                (added_ids, NotificationStage.roll_added),
                (voting_ids, NotificationStage.voting_open),
            ):
                if ids:
                    await session.execute(
                        pg_insert(Notification)
                        .values([{"voter_id": i, "stage": stage} for i in ids])
                        .on_conflict_do_nothing(constraint="uq_notification_voter")
                    )
        sent += 1

    print(f"digest: {sent} person(s) emailed, {failed} failure(s)")
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
        async with job_lock("proposal_digest") as acquired:
            if not acquired:
                print("skipped: another proposal_digest run holds the job lock")
                return 0
            return await main(today=args.today)

    return asyncio.run(locked())


if __name__ == "__main__":
    sys.exit(cli())
