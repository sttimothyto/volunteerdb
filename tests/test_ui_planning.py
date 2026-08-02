"""Planning pages: create from a vacancy, nominate as a voter, vote, appoint.

Phases derive from real dates here (the GUI cannot inject `today`), so each
scenario seeds its proposal through the service with deadlines placed around
local_today().
"""

from datetime import timedelta
from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, planning, teams, users, volunteers

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"


async def _parish(session):
    """Liturgy (Lena leads, Cora core, Mia member) + Clergy (Dan, no account)."""
    liturgy = await teams.create(session, "Liturgy")
    clergy = await teams.create(session, "Clergy")
    lena = await volunteers.create(session, "Lena", "Leader")
    cora = await volunteers.create(session, "Cora", "Core")
    mia = await volunteers.create(session, "Mia", "Member")
    dan = await volunteers.create(session, "Dan", "Deacon")
    vera = await volunteers.create(session, "Vera", "Volunteer")
    await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
    await memberships.assign(session, cora.id, liturgy.id, TeamRole.core)
    await memberships.assign(session, mia.id, liturgy.id, TeamRole.member)
    await memberships.assign(session, dan.id, clergy.id, TeamRole.member)
    await planning.set_config(
        session, planning.PlanningConfig(clergy_team_id=clergy.id)
    )
    lena_u = await users.create(session, "lena@example.org", volunteer_id=lena.id)
    cora_u = await users.create(session, "cora@example.org", volunteer_id=cora.id)
    mia_u = await users.create(session, "mia@example.org", volunteer_id=mia.id)
    admin_u = await users.create(session, "admin@example.org", is_admin=True)
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
    today = planning.local_today()
    async with db_session() as session:
        proposal = await planning.create_proposal(
            session,
            team_id=ids["liturgy"],
            role=TeamRole.second,
            nomination_deadline=today + timedelta(days=d1_offset),
            voting_deadline=today + timedelta(days=d2_offset),
            created_by=ids["admin_u"],
            candidates=[planning.CandidateInput(ids["vera"], "steady hands")],
            today=today + timedelta(days=min(d1_offset, 0)),
        )
        for voter_vol_id, score in (ballots or {}).items():
            view = await planning.detail(session, proposal.id, today=today)
            cand_id = view.candidates[0].candidate.id
            await planning.cast_ballot(
                session,
                proposal.id,
                voter_volunteer_id=voter_vol_id,
                scores={cand_id: score},
                today=today + timedelta(days=d1_offset + 1),
            )
        return proposal.id


async def test_leader_creates_proposal_from_vacancy(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.should_see("dev-login ok")

        await user.open("/planning")
        await user.should_see("Vacancies")
        await user.should_see("no second-in-command")
        user.find("Start proposal", kind=ui.button).click()
        await user.should_see("First candidate")
        who = user.find(kind=ui.select, content="First candidate").elements.pop()
        who.value = ids["vera"]
        user.find("Create proposal", kind=ui.button).click()

        # lands on the detail page: Ignatian reminder, candidate, roll, flags
        await user.should_see("Ignatian election", retries=30)
        await user.should_see("Nominating until")
        await user.should_see("Vera Volunteer")
        await user.should_see("Current commitments:")
        await user.should_see("Dan Deacon")  # clergy joined the roll
        await user.should_see("no account — cannot vote")
        await user.should_see("0 of 3 ballots cast")


async def test_voter_access_and_nomination(database):
    async with db_session() as session:
        ids = await _parish(session)
        victor = await volunteers.create(session, "Victor", "Volunteer")
        victor_id = victor.id
    pid = await _seed_proposal(ids, d1_offset=5, d2_offset=15)

    async with user_simulation(main_file=SIM_MAIN) as user:
        # Mia is a plain member: no roll, no nav, polite refusal
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/planning")
        await user.should_see("Planning is available to admins")
        await user.open(f"/planning/{pid}")
        await user.should_see("visible to its voting members")

        # Cora sits on the roll as a core member: sees, opens, nominates
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open("/planning")
        await user.should_see("Liturgy: Second-in-command")
        await user.open(f"/planning/{pid}")
        await user.should_see("Ignatian election")
        who = user.find(kind=ui.select, content="New candidate").elements.pop()
        who.value = victor_id
        user.find("Nominate", kind=ui.button).click()
        await user.should_see("nominated by cora@example.org", retries=30)


async def test_voting_phase_ballot(database):
    async with db_session() as session:
        ids = await _parish(session)
    pid = await _seed_proposal(ids, d1_offset=-1, d2_offset=6)

    async with user_simulation(main_file=SIM_MAIN) as user:
        # the admin manages but does not vote: no ballot form, no tally yet
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/planning/{pid}")
        await user.should_see("Voting until")
        await user.should_see("Voting is in progress")
        await user.should_not_see("Your ballot")
        await user.should_not_see("Result")

        # Cora votes; scores stay secret, turnout ticks up
        await user.open(f"/login-dev/{ids['cora_u']}")
        await user.open(f"/planning/{pid}")
        await user.should_see("Your ballot")
        await user.should_see("spoiler effect")
        toggle = user.find(kind=ui.toggle).elements.pop()
        toggle.value = 5
        user.find("Submit ballot", kind=ui.button).click()
        await user.should_see("Ballot recorded", retries=30)
        await user.should_see("1 of 3 ballots cast", retries=30)


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
        await user.open(f"/planning/{pid}")
        await user.should_see("Awaiting decision")
        await user.should_see("Result")
        await user.should_see("STAR winner: Vera Volunteer")
        await user.should_see("2 ballots cast")
        await user.should_see("Start new round")

        user.find("Appoint", kind=ui.button).click()
        await user.should_see("This assigns the role immediately")
        user.find("Yes, appoint", kind=ui.button).click()
        await user.should_see("Appointed", retries=30)

        await user.open(f"/teams/{ids['liturgy']}")
        await user.should_see("Vera Volunteer")


async def test_clergy_card_enforces_the_name_invariant(database):
    """The card is the only writer of clergy_team_id, so the picker offers
    nothing the service would reject — and rejects it anyway if forced."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/planning")
        await user.should_see("Clergy team")
        select = user.find(kind=ui.select, content="Clergy team").elements.pop()
        assert set(select.options.values()) == {"— none —", "Clergy"}, (
            "Liturgy is a team but not a candidate for the clergy seat"
        )

        # the picker cannot offer it; the service refuses it regardless
        select.value = ids["liturgy"]
        user.find("Save", kind=ui.button).click()
        await user.should_see("must be the team named", retries=30)

    async with db_session() as session:
        await planning.set_config(session, planning.PlanningConfig(clergy_team_id=None))
        await teams.delete(session, ids["clergy"])  # only allowed once unset

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/planning")
        await user.should_see("No team named")
