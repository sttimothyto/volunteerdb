"""The invariants the database now holds, rather than the services alone.

Each of these was true before — a service maintained it, a comment asserted it —
and storable the other way round. The point of the tests is that the *database*
refuses now, so a future caller that reaches past the service (a Core UPDATE, a
migration, a psql session at 2am) cannot leave the row in a state some reader
treats one way and another reads the opposite.

Every check is written as "the wrong row is refused", because a test that only
inserts correct rows passes with no constraint at all.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from volunteerdb.db import db_session
from volunteerdb.models import (
    AppUser,
    Event,
    EventAssignment,
    EventSlot,
    Interest,
    Proposal,
    ProposalBallot,
    Team,
    Volunteer,
)


async def _refused(stmt) -> str:
    """Run `stmt` in its own session and return the constraint it violated."""
    with pytest.raises(IntegrityError) as caught:
        async with db_session() as session:
            await session.execute(stmt)
    return str(caught.value)


async def test_a_ballot_cannot_score_another_proposals_candidate(database):
    """proposal_ballot.proposal_id is denormalized so a tally is one query. The
    composite foreign keys are what stop it disagreeing with the voter and
    candidate rows it duplicates — otherwise a ballot could be counted in a
    tally it was never cast in."""
    async with db_session() as session:
        team = await session.scalar(
            sa.insert(Team).values(name="Liturgy").returning(Team.id)
        )
        vol = await session.scalar(
            sa.insert(Volunteer)
            .values(first_name="Ann", last_name="Able")
            .returning(Volunteer.id)
        )
        today = datetime.now(UTC).date()
        ids = []
        for role in ("leader", "second"):
            ids.append(
                await session.scalar(
                    sa.insert(Proposal)
                    .values(
                        team_id=team,
                        role=role,
                        nomination_deadline=today,
                        voting_deadline=today + timedelta(days=7),
                    )
                    .returning(Proposal.id)
                )
            )
        first, second = ids
        voter = await session.scalar(
            sa.text(
                "INSERT INTO proposal_voter (proposal_id, volunteer_id)"
                " VALUES (:p, :v) RETURNING id"
            ).bindparams(p=first, v=vol)
        )
        candidate = await session.scalar(
            sa.text(
                "INSERT INTO proposal_candidate (proposal_id, volunteer_id)"
                " VALUES (:p, :v) RETURNING id"
            ).bindparams(p=second, v=vol)
        )

    # the voter is on `first`'s roll, the candidate stands in `second`
    detail = await _refused(
        sa.insert(ProposalBallot).values(
            proposal_id=first, voter_id=voter, candidate_id=candidate, score=5
        )
    )
    assert "fk_ballot_candidate_proposal" in detail


async def test_an_assignment_cannot_hold_another_events_slot(database):
    """event_assignment.event_id is denormalized so per-event reads and the
    one-slot-per-person unique are direct. Without the composite FK somebody
    could sit in slot A of event B, and uq_event_assignment would guard the
    wrong pairing."""
    async with db_session() as session:
        team = await session.scalar(
            sa.insert(Team).values(name="Liturgy").returning(Team.id)
        )
        vol = await session.scalar(
            sa.insert(Volunteer)
            .values(first_name="Ann", last_name="Able")
            .returning(Volunteer.id)
        )
        starts = datetime.now(UTC) + timedelta(days=7)
        events = [
            await session.scalar(
                sa.insert(Event)
                .values(
                    team_id=team,
                    title=title,
                    starts_at=starts,
                    ends_at=starts + timedelta(hours=2),
                )
                .returning(Event.id)
            )
            for title in ("Mass", "Picnic")
        ]
        slot_of_second = await session.scalar(
            sa.insert(EventSlot)
            .values(event_id=events[1], name="Lector")
            .returning(EventSlot.id)
        )

    detail = await _refused(
        sa.insert(EventAssignment).values(
            slot_id=slot_of_second, event_id=events[0], volunteer_id=vol, kind="signup"
        )
    )
    assert "fk_assignment_slot_event" in detail


async def test_an_invite_token_cannot_outlive_its_expiry(database):
    """The pair is set and cleared together (services/users.py), and every
    reader treats a token with no live expiry as dead. Storing one was a way to
    have a link that half the code thought was live."""
    detail = await _refused(
        sa.insert(AppUser).values(
            email="orphan@example.org", invite_token="x" * 64, invite_expires_at=None
        )
    )
    assert "ck_app_user_invite_pair" in detail


async def test_a_pending_address_needs_its_whole_triple(database):
    detail = await _refused(
        sa.insert(AppUser).values(
            email="half@example.org", pending_email="new@example.org"
        )
    )
    assert "ck_app_user_email_change_triple" in detail


async def test_an_address_cannot_be_stored_in_mixed_case(database):
    """Every lookup folds case, and uq_interest_open indexes the raw column, so
    one mixed-case row would have been invisible to the dedup and to search."""
    detail = await _refused(sa.insert(AppUser).values(email="Shouty@Example.ORG"))
    assert "ck_app_user_email_lower" in detail

    async with db_session() as session:
        team = await session.scalar(
            sa.insert(Team).values(name="Liturgy").returning(Team.id)
        )
    detail = await _refused(
        sa.insert(Interest).values(team_id=team, name="Ann", email="Ann@Example.ORG")
    )
    assert "ck_interest_email_lower" in detail


async def test_a_handler_cannot_be_recorded_without_a_handling(database):
    """One-directional on purpose: resolved_by is ON DELETE SET NULL, so a
    resolved submission may lose its handler and keep the time. The reverse — a
    handler with no moment — is what cannot happen."""
    async with db_session() as session:
        team = await session.scalar(
            sa.insert(Team).values(name="Liturgy").returning(Team.id)
        )
        user = await session.scalar(
            sa.insert(AppUser).values(email="admin@example.org").returning(AppUser.id)
        )
    detail = await _refused(
        sa.insert(Interest).values(
            team_id=team, name="Ann", email="ann@example.org", resolved_by=user
        )
    )
    assert "ck_interest_resolution" in detail

    # ...and the SET NULL shape stays storable
    async with db_session() as session:
        await session.execute(
            sa.insert(Interest).values(
                team_id=team,
                name="Bea",
                email="bea@example.org",
                resolved_at=sa.func.now(),
                resolved_by=None,
            )
        )


async def test_a_cancelled_event_records_when(database):
    async with db_session() as session:
        team = await session.scalar(
            sa.insert(Team).values(name="Liturgy").returning(Team.id)
        )
    starts = datetime.now(UTC) + timedelta(days=7)
    detail = await _refused(
        sa.insert(Event).values(
            team_id=team,
            title="Mass",
            starts_at=starts,
            ends_at=starts + timedelta(hours=2),
            status="cancelled",
        )
    )
    assert "ck_event_cancelled_at" in detail


async def test_a_task_force_cannot_be_its_own_owner(database):
    """Teardown restores event.team_id = owner_team_id and then deletes the meta
    team. An equal pair would be a silent no-op that takes the event — and its
    whole attendance record — with it."""
    async with db_session() as session:
        team = await session.scalar(
            sa.insert(Team).values(name="Liturgy").returning(Team.id)
        )
        starts = datetime.now(UTC) + timedelta(days=7)
        event = await session.scalar(
            sa.insert(Event)
            .values(
                team_id=team,
                title="Picnic",
                starts_at=starts,
                ends_at=starts + timedelta(hours=2),
            )
            .returning(Event.id)
        )
    detail = await _refused(
        sa.text(
            "INSERT INTO event_task_force (event_id, team_id, owner_team_id)"
            " VALUES (:e, :t, :t)"
        ).bindparams(e=event, t=team)
    )
    assert "ck_task_force_teams" in detail
