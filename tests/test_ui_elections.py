"""Elections pages: create from a vacancy, nominate as a voter, vote, appoint.

Phases derive from real dates here (the GUI cannot inject `today`), so each
scenario seeds its proposal through the service with deadlines placed around
local_today().
"""

from datetime import timedelta

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.services import elections, memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok


async def _parish(session):
    """Liturgy (Lena leads, Cora core, Mia member) + Clergy (Dan, no account)."""
    liturgy = ok(await teams.create(session, None, "Liturgy"))
    clergy = ok(await teams.create(session, None, "Clergy"))
    lena = ok(await volunteers.create(session, None, "Lena", "Leader"))
    cora = ok(await volunteers.create(session, None, "Cora", "Core"))
    mia = ok(await volunteers.create(session, None, "Mia", "Member"))
    dan = ok(await volunteers.create(session, None, "Dan", "Deacon"))
    vera = ok(await volunteers.create(session, None, "Vera", "Volunteer"))
    ok(await memberships.assign(session, None, lena.id, liturgy.id, TeamRole.leader))
    ok(await memberships.assign(session, None, cora.id, liturgy.id, TeamRole.core))
    ok(await memberships.assign(session, None, mia.id, liturgy.id, TeamRole.member))
    ok(await memberships.assign(session, None, dan.id, clergy.id, TeamRole.member))
    lena_u, _ = ok(
        await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            invite=mint.fresh_invite(),
        )
    )
    cora_u, _ = ok(
        await users.create(
            session,
            "cora@example.org",
            volunteer_id=cora.id,
            invite=mint.fresh_invite(),
        )
    )
    mia_u, _ = ok(
        await users.create(
            session, "mia@example.org", volunteer_id=mia.id, invite=mint.fresh_invite()
        )
    )
    admin_u, _ = ok(
        await users.create(
            session, "admin@example.org", is_admin=True, invite=mint.fresh_invite()
        )
    )
    return {
        "liturgy": liturgy.id,
        "clergy": clergy.id,
        "lena": lena.id,
        "cora": cora.id,
        "vera": vera.id,
        "lena_u": lena_u.id,
        "cora_u": cora_u.id,
        "mia_u": mia_u.id,
        "admin_u": admin_u.id,
    }


async def _seed_proposal(ids, *, d1_offset: int, d2_offset: int, ballots=None):
    """A Liturgy/second proposal with Vera as candidate, deadlines placed
    relative to local_today(); optional ballots cast while backdated."""
    today = mint.today()
    async with db_session() as session:
        proposal = ok(
            await elections.create_proposal(
                session,
                None,
                team_id=ids["liturgy"],
                role=TeamRole.second,
                nomination_deadline=today + timedelta(days=d1_offset),
                voting_deadline=today + timedelta(days=d2_offset),
                created_by=ids["admin_u"],
                candidates=[elections.CandidateInput(ids["vera"], "steady hands")],
                today=today + timedelta(days=min(d1_offset, 0)),
            )
        )
        for voter_vol_id, score in (ballots or {}).items():
            view = ok(await elections.detail(session, None, proposal.id, today=today))
            cand_id = view.candidates[0].candidate.id
            ok(
                await elections.cast_ballot(
                    session,
                    None,
                    proposal.id,
                    voter_volunteer_id=voter_vol_id,
                    scores={cand_id: score},
                    today=today + timedelta(days=d1_offset + 1),
                    now=mint.now(),
                )
            )
        return proposal.id


async def test_leader_creates_proposal_from_vacancy(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.should_see("dev-login ok")

        await user.open("/elections")
        await user.should_see("Vacancies")
        await user.should_see("no second-in-command")
        user.find("Start proposal", kind=ui.button).click()
        await user.should_see("First candidate")
        who = only(user.find(kind=ui.select, content="First candidate"))
        who.value = ids["vera"]
        user.find("Create proposal", kind=ui.button).click()

        # lands on the detail page: Ignatian reminder, candidate, roll, flags
        await user.should_see("Ignatian election", retries=SLOW)
        await user.should_see("Nominating until")
        await user.should_see("Vera Volunteer")
        await user.should_see("Current commitments:")
        await user.should_see("Dan Deacon")  # clergy joined the roll
        await user.should_see("no account — cannot vote")
        await user.should_see("0 of 3 ballots cast")


async def test_voter_access_and_nomination(database):
    async with db_session() as session:
        ids = await _parish(session)
        victor = ok(await volunteers.create(session, None, "Victor", "Volunteer"))
        victor_id = victor.id
    pid = await _seed_proposal(ids, d1_offset=5, d2_offset=15)

    async with user_simulation(main_file=SIM_MAIN) as user:
        # Mia is a plain member: no roll, no nav, polite refusal
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/elections")
        await user.should_see("Elections are available to admins")
        await user.open(f"/elections/{pid}")
        await user.should_see("visible to its voting members")

        # Cora sits on the roll as a core member: sees, opens, nominates
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open("/elections")
        await user.should_see("Liturgy: Second-in-command")
        await user.open(f"/elections/{pid}")
        await user.should_see("Ignatian election")
        who = only(user.find(kind=ui.select, content="New candidate"))
        who.value = victor_id
        user.find("Nominate", kind=ui.button).click()
        await user.should_see("nominated by cora@example.org", retries=SLOW)


async def test_voting_phase_ballot(database):
    async with db_session() as session:
        ids = await _parish(session)
    pid = await _seed_proposal(ids, d1_offset=-1, d2_offset=6)

    async with user_simulation(main_file=SIM_MAIN) as user:
        # the admin manages but does not vote: no ballot form, no tally yet
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/elections/{pid}")
        await user.should_see("Voting until")
        await user.should_see("Voting is in progress")
        await user.should_not_see("Your ballot")
        await user.should_not_see("Result")

        # Cora votes; scores stay secret, turnout ticks up
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open(f"/elections/{pid}")
        await user.should_see("Your ballot")
        await user.should_see("spoiler effect")
        toggle = only(user.find(kind=ui.toggle))
        toggle.value = 5
        user.find("Submit ballot", kind=ui.button).click()
        await user.should_see("Ballot recorded", retries=SLOW)
        await user.should_see("1 of 3 ballots cast", retries=SLOW)


async def test_concluded_tally_and_appointment(database):
    async with db_session() as session:
        ids = await _parish(session)
    pid = await _seed_proposal(
        ids,
        d1_offset=-10,
        d2_offset=-2,
        ballots={ids["lena"]: 4, ids["cora"]: 5},
    )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/elections/{pid}")
        await user.should_see("Awaiting decision")
        await user.should_see("Result")
        await user.should_see("STAR winner: Vera Volunteer")
        await user.should_see("2 ballots cast")
        await user.should_see("Start new round")

        user.find("Appoint", kind=ui.button).click()
        await user.should_see("This assigns the role immediately")
        user.find("Yes, appoint", kind=ui.button).click()
        await user.should_see("Appointed", retries=SLOW)

        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Vera Volunteer")


async def test_profile_lists_proposals_involving_the_volunteer(database):
    """The volunteer detail page shows the proposals touching them, with one
    badge per kind of involvement, scoped to what the viewer may see."""
    async with db_session() as session:
        ids = await _parish(session)

    # a decided round first (appointing frees the open-proposal slot), then a
    # fresh nominating round for the same seat — Vera is candidate on both,
    # and as the newly appointed second she sits on the fresh round's roll
    pid_done = await _seed_proposal(ids, d1_offset=-10, d2_offset=-5)
    async with db_session() as session:
        view = ok(await elections.detail(session, None, pid_done, today=mint.today()))
        ok(
            await elections.appoint(
                session,
                None,
                pid_done,
                view.candidates[0].candidate.id,
                decided_by=ids["admin_u"],
                today=mint.today(),
                now=mint.now(),
            )
        )
    await _seed_proposal(ids, d1_offset=5, d2_offset=10)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.should_see("dev-login ok")

        await user.open(f"/volunteers/{ids['vera']}")
        await user.should_see("Proposals involving them")
        await user.should_see("Liturgy: Second-in-command")
        await user.should_see("Appointed")  # the decided round
        await user.should_see("Candidate")  # the fresh round
        await user.should_see("Nominating until")
        await user.should_see("Voting member")  # on the fresh roll as second

        # Lena sits on both rolls but was never nominated
        await user.open(f"/volunteers/{ids['lena']}")
        await user.should_see("Voting member")
        await user.should_not_see("Candidate")

        # a plain member has no elections access: no section at all
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.should_see("dev-login ok")
        await user.open(f"/volunteers/{ids['vera']}")
        await user.should_see("Vera Volunteer")
        await user.should_not_see("Proposals involving them")
