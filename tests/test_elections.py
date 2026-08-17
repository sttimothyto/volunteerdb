"""Elections service: the nomination + STAR-voting pipeline.

Every phase-dependent call takes an explicit `today`, so a proposal is
walked through nominating -> voting -> concluded without touching the
clock: TODAY < D1 < VOTING_DAY < D2 < AFTER.
"""

from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from volunteerdb.db import db_session
from volunteerdb.models import AppUser, ProposalStatus, TeamRole
from volunteerdb.permissions import load_actor
from volunteerdb.services import elections, memberships, teams, users, volunteers

TODAY = date(2026, 8, 10)  # nominating
D1 = date(2026, 8, 15)  # nomination deadline
VOTING_DAY = date(2026, 8, 20)  # voting
D2 = date(2026, 8, 25)  # voting deadline
AFTER = date(2026, 8, 30)  # concluded


@dataclass
class Parish:
    liturgy_id: int
    garden_id: int
    clergy_id: int
    lena_id: int  # Liturgy leader, has an account
    cora_id: int  # Liturgy core member, has an account
    mia_id: int  # Liturgy plain member
    pete_id: int  # Clergy member, has an account
    dan_id: int  # Clergy member, NO account
    vera_id: int  # unattached candidate
    victor_id: int  # unattached candidate
    lena_user_id: int
    admin_user_id: int
    pete_user_id: int


async def _parish(session) -> Parish:
    """Liturgy has a leader but no second; Garden has nobody; Clergy is the
    team the roll builder finds by name."""
    liturgy = await teams.create(session, "Liturgy")
    garden = await teams.create(session, "Garden")
    clergy = await teams.create(session, "Clergy")
    lena = await volunteers.create(session, "Lena", "Leader")
    cora = await volunteers.create(session, "Cora", "Core")
    mia = await volunteers.create(session, "Mia", "Member")
    pete = await volunteers.create(session, "Pete", "Priest")
    dan = await volunteers.create(session, "Dan", "Deacon")
    vera = await volunteers.create(session, "Vera", "Volunteer")
    victor = await volunteers.create(session, "Victor", "Volunteer")
    await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
    await memberships.assign(session, cora.id, liturgy.id, TeamRole.core)
    await memberships.assign(session, mia.id, liturgy.id, TeamRole.member)
    await memberships.assign(session, pete.id, clergy.id, TeamRole.member)
    await memberships.assign(session, dan.id, clergy.id, TeamRole.member)
    lena_user = await users.create(session, "lena@example.org", volunteer_id=lena.id)
    await users.create(session, "cora@example.org", volunteer_id=cora.id)
    pete_user = await users.create(session, "pete@example.org", volunteer_id=pete.id)
    admin_user = await users.create(session, "admin@example.org", is_admin=True)
    return Parish(
        liturgy_id=liturgy.id,
        garden_id=garden.id,
        clergy_id=clergy.id,
        lena_id=lena.id,
        cora_id=cora.id,
        mia_id=mia.id,
        pete_id=pete.id,
        dan_id=dan.id,
        vera_id=vera.id,
        victor_id=victor.id,
        lena_user_id=lena_user.id,
        admin_user_id=admin_user.id,
        pete_user_id=pete_user.id,
    )


async def _open_proposal(session, p: Parish, *, team_id=None, candidates=None):
    return await elections.create_proposal(
        session,
        team_id=team_id or p.liturgy_id,
        role=TeamRole.second,
        nomination_deadline=D1,
        voting_deadline=D2,
        created_by=p.admin_user_id,
        candidates=candidates or [elections.CandidateInput(p.vera_id, "steady hands")],
        today=TODAY,
    )


async def _actor(session, user_id: int):
    return await load_actor(session, await session.get(AppUser, user_id))


# --- creation and the voting-roll template -----------------------------------


async def test_default_roll_leadership_core_plus_clergy(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        view = await elections.detail(session, proposal.id, today=TODAY)
        roll = {v.volunteer.id for v in view.voters}
        assert roll == {p.lena_id, p.cora_id, p.pete_id, p.dan_id}, (
            "leader + core of the target team plus all clergy; plain members excluded"
        )
        assert view.phase == elections.ProposalPhase.nominating
        assert view.creator_email == "admin@example.org"
        by_vol = {v.volunteer.id: v for v in view.voters}
        assert by_vol[p.pete_id].has_account
        assert not by_vol[p.dan_id].has_account, "Dan has no account, cannot vote"
        assert not any(v.has_voted for v in view.voters)


async def test_default_roll_without_a_clergy_team(database):
    """No team named "Clergy" is not an error: the roll is simply the target
    team's own leadership and core members."""
    async with db_session() as session:
        p = await _parish(session)
        await teams.delete(session, p.clergy_id)
        proposal = await _open_proposal(session, p)
        view = await elections.detail(session, proposal.id, today=TODAY)
        assert {v.volunteer.id for v in view.voters} == {p.lena_id, p.cora_id}


async def test_roll_dedupes_clergy_who_also_lead(database):
    async with db_session() as session:
        p = await _parish(session)
        await memberships.assign(session, p.pete_id, p.liturgy_id, TeamRole.core)
        proposal = await _open_proposal(session, p)
        view = await elections.detail(session, proposal.id, today=TODAY)
        assert [v.volunteer.id for v in view.voters].count(p.pete_id) == 1


# --- the clergy standing -----------------------------------------------------
#
# One team votes on every proposal, and it is whichever team is named
# "Clergy" when the roll is built. Renaming is how the standing is retired,
# and it only reaches future rolls: an open proposal's roll is already
# materialised as ProposalVoter rows.


async def test_renaming_the_clergy_team_retires_the_standing(database):
    async with db_session() as session:
        p = await _parish(session)
        before = await _open_proposal(session, p)
        await teams.update(session, p.clergy_id, name="Presbyterate")
        after = await _open_proposal(session, p, team_id=p.garden_id)

        rolls = {
            proposal.id: {
                v.volunteer.id
                for v in (
                    await elections.detail(session, proposal.id, today=TODAY)
                ).voters
            }
            for proposal in (before, after)
        }
        assert rolls[before.id] == {p.lena_id, p.cora_id, p.pete_id, p.dan_id}, (
            "an already-open proposal keeps the roll it was created with"
        )
        assert rolls[after.id] == set(), "Garden has no leadership and no clergy join"


async def test_a_team_renamed_to_clergy_takes_up_the_standing(database):
    """Nothing registers the clergy team, so the name alone confers it."""
    async with db_session() as session:
        p = await _parish(session)
        await teams.delete(session, p.clergy_id)
        await teams.update(session, p.liturgy_id, name="Clergy")
        proposal = await _open_proposal(session, p, team_id=p.garden_id)
        view = await elections.detail(session, proposal.id, today=TODAY)
        assert {v.volunteer.id for v in view.voters} == {
            p.lena_id,
            p.cora_id,
            p.mia_id,
        }, "every member of the now-Clergy team, whatever their role"


async def test_create_validations(database):
    async with db_session() as session:
        p = await _parish(session)
        common = dict(
            team_id=p.liturgy_id,
            role=TeamRole.second,
            created_by=p.admin_user_id,
            today=TODAY,
        )
        with pytest.raises(ValueError, match="at least one candidate"):
            await elections.create_proposal(
                session,
                nomination_deadline=D1,
                voting_deadline=D2,
                candidates=[],
                **common,
            )
        with pytest.raises(ValueError, match="only once"):
            await elections.create_proposal(
                session,
                nomination_deadline=D1,
                voting_deadline=D2,
                candidates=[
                    elections.CandidateInput(p.vera_id),
                    elections.CandidateInput(p.vera_id),
                ],
                **common,
            )
        with pytest.raises(ValueError, match="in the past"):
            await elections.create_proposal(
                session,
                nomination_deadline=date(2026, 8, 1),
                voting_deadline=D2,
                candidates=[elections.CandidateInput(p.vera_id)],
                **common,
            )
        with pytest.raises(ValueError, match="must fall after"):
            await elections.create_proposal(
                session,
                nomination_deadline=D1,
                voting_deadline=D1,
                candidates=[elections.CandidateInput(p.vera_id)],
                **common,
            )


async def test_one_open_proposal_per_seat(database):
    async with db_session() as session:
        p = await _parish(session)
        first = await _open_proposal(session, p)
        first_id = first.id

    with pytest.raises(IntegrityError):
        async with db_session() as session:
            await _open_proposal(session, p)  # Parish holds plain ints

    async with db_session() as session:
        await elections.cancel(session, first_id, decided_by=p.admin_user_id)
        again = await _open_proposal(session, p)
        assert again.id != first_id, "the partial unique index only guards OPEN seats"


# --- phase enforcement -------------------------------------------------------


async def test_nomination_window(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        c = await elections.add_candidate(
            session,
            proposal.id,
            volunteer_id=p.victor_id,
            nominated_by=p.pete_user_id,
            note="knows the garden",
            today=D1,  # deadline day itself still nominates
        )
        assert c.note == "knows the garden"
        with pytest.raises(ValueError, match="is voting, not nominating"):
            await elections.add_candidate(
                session,
                proposal.id,
                volunteer_id=p.mia_id,
                nominated_by=p.pete_user_id,
                today=VOTING_DAY,
            )
        with pytest.raises(IntegrityError):
            await elections.add_candidate(
                session,
                proposal.id,
                volunteer_id=p.victor_id,
                nominated_by=p.admin_user_id,
                today=TODAY,
            )


async def test_candidate_removal_only_while_nominating(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        extra = await elections.add_candidate(
            session,
            proposal.id,
            volunteer_id=p.victor_id,
            nominated_by=p.admin_user_id,
            today=TODAY,
        )
        with pytest.raises(ValueError, match="cannot remove a candidate"):
            await elections.remove_candidate(
                session, proposal.id, extra.id, today=VOTING_DAY
            )
        await elections.remove_candidate(session, proposal.id, extra.id, today=TODAY)
        view = await elections.detail(session, proposal.id, today=TODAY)
        assert [c.volunteer.id for c in view.candidates] == [p.vera_id]


async def test_roll_freezes_when_voting_begins(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        added = await elections.add_voter(
            session,
            proposal.id,
            volunteer_id=p.mia_id,
            added_by=p.admin_user_id,
            today=TODAY,
        )
        await elections.remove_voter(session, proposal.id, added.id, today=TODAY)
        with pytest.raises(ValueError, match="cannot add a voter"):
            await elections.add_voter(
                session,
                proposal.id,
                volunteer_id=p.mia_id,
                added_by=p.admin_user_id,
                today=VOTING_DAY,
            )


# --- voting ------------------------------------------------------------------


async def _candidate_ids(session, proposal_id) -> dict[int, int]:
    """volunteer id -> candidate id"""
    view = await elections.detail(session, proposal_id, today=AFTER)
    return {c.volunteer.id: c.candidate.id for c in view.candidates}


async def test_cast_ballot_phase_and_validation(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        cand = await _candidate_ids(session, proposal.id)
        vera_c = cand[p.vera_id]

        with pytest.raises(ValueError, match="is nominating, not voting"):
            await elections.cast_ballot(
                session,
                proposal.id,
                voter_volunteer_id=p.lena_id,
                scores={vera_c: 5},
                today=TODAY,
            )
        with pytest.raises(ValueError, match="is concluded, not voting"):
            await elections.cast_ballot(
                session,
                proposal.id,
                voter_volunteer_id=p.lena_id,
                scores={vera_c: 5},
                today=AFTER,
            )
        with pytest.raises(ValueError, match="between 0 and 5"):
            await elections.cast_ballot(
                session,
                proposal.id,
                voter_volunteer_id=p.lena_id,
                scores={vera_c: 6},
                today=VOTING_DAY,
            )
        with pytest.raises(ValueError, match="not candidates"):
            await elections.cast_ballot(
                session,
                proposal.id,
                voter_volunteer_id=p.lena_id,
                scores={424242: 3},
                today=VOTING_DAY,
            )
        with pytest.raises(LookupError, match="voting roll"):
            await elections.cast_ballot(
                session,
                proposal.id,
                voter_volunteer_id=p.mia_id,
                scores={vera_c: 3},
                today=VOTING_DAY,
            )


async def test_ballot_revision_and_zero_defaults(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        await elections.add_candidate(
            session,
            proposal.id,
            volunteer_id=p.victor_id,
            nominated_by=p.admin_user_id,
            today=TODAY,
        )
        cand = await _candidate_ids(session, proposal.id)

        # missing keys are written as explicit 0s — an all-zero ballot counts
        await elections.cast_ballot(
            session,
            proposal.id,
            voter_volunteer_id=p.lena_id,
            scores={},
            today=VOTING_DAY,
        )
        assert await elections.my_scores(session, proposal.id, p.lena_id) == {
            cand[p.vera_id]: 0,
            cand[p.victor_id]: 0,
        }
        view = await elections.detail(session, proposal.id, today=VOTING_DAY)
        by_vol = {v.volunteer.id: v for v in view.voters}
        assert by_vol[p.lena_id].has_voted
        assert not by_vol[p.cora_id].has_voted
        assert view.tally is None, "no aggregates while voting is open"

        # revision upserts over the previous scores
        await elections.cast_ballot(
            session,
            proposal.id,
            voter_volunteer_id=p.lena_id,
            scores={cand[p.vera_id]: 5, cand[p.victor_id]: 2},
            today=VOTING_DAY,
        )
        assert await elections.my_scores(session, proposal.id, p.lena_id) == {
            cand[p.vera_id]: 5,
            cand[p.victor_id]: 2,
        }


async def test_tally_gated_then_correct(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        await elections.add_candidate(
            session,
            proposal.id,
            volunteer_id=p.victor_id,
            nominated_by=p.admin_user_id,
            today=TODAY,
        )
        cand = await _candidate_ids(session, proposal.id)
        vera_c, victor_c = cand[p.vera_id], cand[p.victor_id]
        ballots = {
            p.lena_id: {vera_c: 5, victor_c: 2},
            p.cora_id: {vera_c: 1, victor_c: 4},
            p.pete_id: {vera_c: 0, victor_c: 3},
        }
        for voter_id, scores in ballots.items():
            await elections.cast_ballot(
                session,
                proposal.id,
                voter_volunteer_id=voter_id,
                scores=scores,
                today=VOTING_DAY,
            )

        with pytest.raises(ValueError, match="has not concluded"):
            await elections.tally(session, proposal.id, today=VOTING_DAY)

        result = await elections.tally(session, proposal.id, today=AFTER)
        assert result.ballot_count == 3
        assert result.totals == {vera_c: 6, victor_c: 9}
        assert result.finalist_ids == (victor_c, vera_c)
        assert result.runoff == {victor_c: 2, vera_c: 1}
        assert result.winner_id == victor_c
        view = await elections.detail(session, proposal.id, today=AFTER)
        assert view.tally == result


# --- decisions ---------------------------------------------------------------


async def test_appoint_concluded_only_and_creates_membership(database):
    async with db_session() as session:
        p = await _parish(session)
        # Vera is already a plain member: appointment upgrades her role
        await memberships.assign(session, p.vera_id, p.liturgy_id, TeamRole.member)
        proposal = await _open_proposal(session, p)
        cand = await _candidate_ids(session, proposal.id)

        with pytest.raises(ValueError, match="cannot appoint"):
            await elections.appoint(
                session,
                proposal.id,
                cand[p.vera_id],
                decided_by=p.admin_user_id,
                today=VOTING_DAY,
            )

        appointed = await elections.appoint(
            session,
            proposal.id,
            cand[p.vera_id],
            decided_by=p.admin_user_id,
            today=AFTER,
        )
        assert appointed.status == ProposalStatus.appointed.value
        assert appointed.appointed_candidate_id == cand[p.vera_id]
        assert appointed.decided_at is not None
        m = await memberships.find(session, p.vera_id, p.liturgy_id)
        assert m.role == TeamRole.second, "assign() upserts: the role is upgraded"

        with pytest.raises(ValueError, match="is appointed"):
            await elections.appoint(
                session,
                proposal.id,
                cand[p.vera_id],
                decided_by=p.admin_user_id,
                today=AFTER,
            )


async def test_appoint_foreign_candidate_refused(database):
    async with db_session() as session:
        p = await _parish(session)
        liturgy_p = await _open_proposal(session, p)
        garden_p = await _open_proposal(session, p, team_id=p.garden_id)
        garden_cand = (await _candidate_ids(session, garden_p.id))[p.vera_id]
        with pytest.raises(LookupError, match="not found"):
            await elections.appoint(
                session,
                liturgy_p.id,
                garden_cand,
                decided_by=p.admin_user_id,
                today=AFTER,
            )


async def test_cancel(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        cancelled = await elections.cancel(
            session, proposal.id, decided_by=p.admin_user_id
        )
        assert cancelled.status == ProposalStatus.cancelled.value
        with pytest.raises(ValueError, match="already cancelled"):
            await elections.cancel(session, proposal.id, decided_by=p.admin_user_id)


async def test_new_round_clones_candidates_and_roll_not_ballots(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        cand = await _candidate_ids(session, proposal.id)
        await elections.cast_ballot(
            session,
            proposal.id,
            voter_volunteer_id=p.lena_id,
            scores={cand[p.vera_id]: 4},
            today=VOTING_DAY,
        )

        with pytest.raises(ValueError, match="cannot start a new round"):
            await elections.new_round(
                session,
                proposal.id,
                created_by=p.admin_user_id,
                nomination_deadline=date(2026, 9, 5),
                voting_deadline=date(2026, 9, 15),
                today=VOTING_DAY,
            )

        fresh = await elections.new_round(
            session,
            proposal.id,
            created_by=p.admin_user_id,
            nomination_deadline=date(2026, 9, 5),
            voting_deadline=date(2026, 9, 15),
            today=AFTER,
        )
        assert fresh.id != proposal.id
        assert fresh.status == ProposalStatus.open.value
        source = await elections.get(session, proposal.id)
        assert source.status == ProposalStatus.cancelled.value

        view = await elections.detail(session, fresh.id, today=AFTER)
        assert [c.volunteer.id for c in view.candidates] == [p.vera_id]
        assert view.candidates[0].candidate.note == "steady hands"
        assert {v.volunteer.id for v in view.voters} == {
            p.lena_id,
            p.cora_id,
            p.pete_id,
            p.dan_id,
        }
        assert not any(v.has_voted for v in view.voters), "ballots never travel"


async def test_update_proposal_guards(database):
    async with db_session() as session:
        p = await _parish(session)
        proposal = await _open_proposal(session, p)
        updated = await elections.update_proposal(
            session,
            proposal.id,
            nomination_deadline=date(2026, 8, 18),
            notes="take our time",
            today=TODAY,
        )
        assert updated.nomination_deadline == date(2026, 8, 18)
        assert updated.notes == "take our time"

        with pytest.raises(ValueError, match="must fall after"):
            await elections.update_proposal(
                session, proposal.id, voting_deadline=date(2026, 8, 17), today=TODAY
            )

        cand = await _candidate_ids(session, proposal.id)
        await elections.cast_ballot(
            session,
            proposal.id,
            voter_volunteer_id=p.lena_id,
            scores={cand[p.vera_id]: 4},
            today=date(2026, 8, 20),
        )
        with pytest.raises(ValueError, match="cannot reopen"):
            await elections.update_proposal(
                session,
                proposal.id,
                nomination_deadline=date(2026, 8, 22),
                today=date(2026, 8, 21),
            )
        # extending only the voting deadline is fine even with ballots cast
        await elections.update_proposal(
            session,
            proposal.id,
            voting_deadline=date(2026, 8, 28),
            today=date(2026, 8, 21),
        )

        await elections.cancel(session, proposal.id, decided_by=p.admin_user_id)
        with pytest.raises(ValueError, match="already cancelled"):
            await elections.update_proposal(
                session, proposal.id, notes="too late", today=TODAY
            )


# --- listing and scoping -----------------------------------------------------


async def test_list_proposals_scoping(database):
    async with db_session() as session:
        p = await _parish(session)
        liturgy_p = await _open_proposal(session, p)
        garden_p = await _open_proposal(session, p, team_id=p.garden_id)

        admin = await _actor(session, p.admin_user_id)
        assert {
            s.proposal.id
            for s in await elections.list_proposals(session, admin, today=TODAY)
        } == {liturgy_p.id, garden_p.id}

        lena = await _actor(session, p.lena_user_id)
        lena_rows = await elections.list_proposals(session, lena, today=TODAY)
        assert {s.proposal.id for s in lena_rows} == {liturgy_p.id}, (
            "Lena manages Liturgy and is not on Garden's roll"
        )
        assert lena_rows[0].phase == elections.ProposalPhase.nominating
        assert lena_rows[0].candidate_count == 1
        assert lena_rows[0].voter_count == 4

        pete = await _actor(session, p.pete_user_id)
        assert {
            s.proposal.id
            for s in await elections.list_proposals(session, pete, today=TODAY)
        } == {liturgy_p.id, garden_p.id}, "clergy sit on every template roll"

        await elections.cancel(session, garden_p.id, decided_by=p.admin_user_id)
        open_only = await elections.list_proposals(
            session, admin, status=ProposalStatus.open.value, today=TODAY
        )
        assert [s.proposal.id for s in open_only] == [liturgy_p.id]


async def test_involving_flags_and_scoping(database):
    async with db_session() as session:
        p = await _parish(session)
        liturgy_p = await _open_proposal(session, p)
        garden_p = await _open_proposal(session, p, team_id=p.garden_id)

        admin = await _actor(session, p.admin_user_id)
        # Vera: a candidate on both seats, on neither roll
        inv = await elections.involving(session, admin, p.vera_id, today=TODAY)
        assert {i.proposal.id for i in inv} == {liturgy_p.id, garden_p.id}
        assert all(i.as_candidate and not i.as_voter and not i.appointed for i in inv)
        assert all(i.phase == elections.ProposalPhase.nominating for i in inv)

        # Lena: on Liturgy's roll (leader), not a candidate anywhere
        inv = await elections.involving(session, admin, p.lena_id, today=TODAY)
        assert [i.proposal.id for i in inv] == [liturgy_p.id]
        assert inv[0].as_voter and not inv[0].as_candidate

        # Mia touches no proposal at all
        assert await elections.involving(session, admin, p.mia_id, today=TODAY) == []

        # actor scoping mirrors list_proposals: Lena manages only Liturgy,
        # so Vera's Garden candidacy stays hidden from her
        lena = await _actor(session, p.lena_user_id)
        inv = await elections.involving(session, lena, p.vera_id, today=TODAY)
        assert [i.proposal.id for i in inv] == [liturgy_p.id]

        # a plain member has no elections access: nothing, not even about herself
        mia_user = await users.create(session, "mia@example.org", volunteer_id=p.mia_id)
        mia = await _actor(session, mia_user.id)
        assert await elections.involving(session, mia, p.vera_id, today=TODAY) == []

        # appointment flips the winner's flag on that proposal only
        cand = await _candidate_ids(session, liturgy_p.id)
        await elections.appoint(
            session,
            liturgy_p.id,
            cand[p.vera_id],
            decided_by=p.admin_user_id,
            today=AFTER,
        )
        inv = await elections.involving(session, admin, p.vera_id, today=AFTER)
        appointed = {i.proposal.id: i.appointed for i in inv}
        assert appointed == {liturgy_p.id: True, garden_p.id: False}
        assert next(i for i in inv if i.proposal.id == liturgy_p.id).phase is None
