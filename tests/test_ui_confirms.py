"""Anything that removes a record or takes a person off something asks first.

The rule (uiux-improvement.md, Decisions) is held mechanically by the sweep
in test_ui_layer.py; what that cannot see is the wording and the two answers.
So this walks the questions a reader actually meets -- the roster, the
profile, a shift, a slot, a team, an account's re-invite, a photo, a
candidate -- and checks that "Cancel" changes nothing and the affirmative,
which names the verb and the object, does the deed.

Every question carries the same two markers (forms.confirm), so a test
answers it without knowing the button's words.
"""

from datetime import UTC, datetime, timedelta
from io import BytesIO

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation
from PIL import Image

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import elections as elections_service
from volunteerdb.services import events as event_service
from volunteerdb.services import memberships, photos, teams, users, volunteers

from tests import mint
from tests.actors import as_volunteer
from tests.conftest import SIM_MAIN, SLOW, db_session, mail_to, only
from tests.fp_helpers import ok

YES, NO = "confirm-yes", "confirm-no"


async def _parish(session) -> dict[str, int]:
    """Music (Lena leads) with Mia on it; an admin; a next-week event with a
    Lector slot Mia holds and an empty Usher slot."""
    music = ok(await teams.create(session, SYSTEM, "Music"))
    lena = ok(
        await volunteers.create(session, SYSTEM, "Lena", "Leader", "lena@example.org")
    )
    mia = ok(
        await volunteers.create(session, SYSTEM, "Mia", "Member", "mia@example.org")
    )
    lena_m = ok(
        await memberships.assign(session, SYSTEM, lena.id, music.id, TeamRole.leader)
    )
    mia_m = ok(
        await memberships.assign(session, SYSTEM, mia.id, music.id, TeamRole.member)
    )
    admin, _ = ok(
        await users.create(
            session,
            "admin@example.org",
            is_admin=True,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    lena_u, _ = ok(
        await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    mia_u, _ = ok(
        await users.create(
            session,
            "mia@example.org",
            volunteer_id=mia.id,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    starts = mint.now().replace(hour=15, minute=0) + timedelta(days=7)
    created = ok(
        await event_service.create_event(
            session,
            SYSTEM,
            team_id=music.id,
            title="Sunday Mass",
            starts_at=starts,
            ends_at=starts + timedelta(hours=2),
            slots=[
                event_service.SlotInput("Lector", 2),
                event_service.SlotInput("Usher", 1),
            ],
            created_by=None,
            tz=mint.tz(),
            series_id=mint.uuid(),
        )
    )
    event = created[0]
    view = ok(await event_service.detail(session, SYSTEM, event.id))
    lector = next(sv.slot for sv in view.slots if sv.slot.name == "Lector")
    usher = next(sv.slot for sv in view.slots if sv.slot.name == "Usher")
    assignment = ok(
        await event_service.sign_up(
            session,
            as_volunteer(mia.id),
            slot_id=lector.id,
            volunteer_id=mia.id,
            now=mint.now(),
        )
    )
    return {
        "music": music.id,
        "lena": lena.id,
        "mia": mia.id,
        "lena_m": lena_m.id,
        "mia_m": mia_m.id,
        "admin_u": admin.id,
        "lena_u": lena_u.id,
        "mia_u": mia_u.id,
        "event": event.id,
        "usher": usher.id,
        "assignment": assignment.id,
    }


async def test_removing_from_the_roster_asks_and_cancel_keeps_them(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        user.find(marker=f"remove-member-{ids['mia_m']}").click()
        await user.should_see("Remove Mia Member from the Music roster?", retries=SLOW)
        await user.should_see("The history keeps the membership.")
        user.find(marker=NO).click()
        await user.should_see("Roster")

    async with db_session() as session:
        assert await memberships.get(session, ids["mia_m"]) is not None, (
            "Cancel changed nothing"
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        user.find(marker=f"remove-member-{ids['mia_m']}").click()
        await user.should_see("Remove Mia Member from the Music roster?", retries=SLOW)
        # the affirmative names the verb and the object
        yes = only(user.find(marker=YES))
        assert yes.text == "Remove Mia Member from Music"
        user.find(marker=YES).click()
        await user.should_not_see(marker=f"remove-member-{ids['mia_m']}", retries=SLOW)

    async with db_session() as session:
        assert await memberships.get(session, ids["mia_m"]) is None


async def test_the_profile_page_asks_the_same_question(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/volunteers/{ids['mia']}")
        user.find(marker=f"remove-member-{ids['mia_m']}").click()
        await user.should_see("Remove Mia Member from the Music roster?", retries=SLOW)
        user.find(marker=YES).click()
        await user.should_see("Not on any team.", retries=SLOW)


async def test_a_leader_taking_somebody_off_a_shift_is_asked(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{ids['event']}")
        await user.should_see("1/2")
        user.find(marker=f"remove-assignment-{ids['assignment']}").click()
        await user.should_see("Remove Mia Member from the Lector slot?", retries=SLOW)
        await user.should_see("Nobody is emailed.")
        user.find(marker=NO).click()
        await user.should_see("1/2")
        user.find(marker=f"remove-assignment-{ids['assignment']}").click()
        await user.should_see("Remove Mia Member from the Lector slot?", retries=SLOW)
        user.find(marker=YES).click()
        await user.should_see("0/2", retries=SLOW)


async def test_deleting_an_empty_slot_is_asked(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{ids['event']}")
        await user.should_see("Usher")
        user.find(marker=f"slot-delete-{ids['usher']}").click()
        await user.should_see("Delete the slot Usher?", retries=SLOW)
        assert only(user.find(marker=YES)).text == "Delete the slot"
        user.find(marker=YES).click()
        await user.should_not_see("Usher", retries=SLOW)


async def test_deleting_a_team_names_the_roster_it_takes_with_it(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open(f"/teams/{ids['music']}")
        user.find("Delete", kind=ui.button).click()
        await user.should_see("Delete the team Music?", retries=SLOW)
        await user.should_see("Its 2 roster places go with it.")
        user.find(marker=NO).click()
        await user.should_see("Roster")

        user.find("Delete", kind=ui.button).click()
        await user.should_see("Delete the team Music?", retries=SLOW)
        assert only(user.find(marker=YES)).text == "Delete the team"
        user.find(marker=YES).click()
        # the listing, with the parish's one team gone
        await user.should_see("No teams yet.", retries=SLOW)


async def test_a_new_invite_link_is_asked_because_it_resets_the_password(
    database, sim_sent
):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/admin/users")
        user.find(marker=f"reinvite-{ids['mia_u']}").click()
        await user.should_see("Send mia@example.org a new invite link?", retries=SLOW)
        await user.should_see("Their password is removed")
        user.find(marker=NO).click()
        await user.should_see("3 accounts")
        assert sim_sent == [], "Cancel mailed nobody"

        user.find(marker=f"reinvite-{ids['mia_u']}").click()
        await user.should_see("Send mia@example.org a new invite link?", retries=SLOW)
        assert only(user.find(marker=YES)).text == "Send a new invite link"
        user.find(marker=YES).click()
        await user.should_see("Invite link for mia@example.org", retries=SLOW)
        await mail_to(sim_sent, "mia@example.org")


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (60, 60), (200, 120, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_removing_a_photo_is_asked(database):
    async with db_session() as session:
        ids = await _parish(session)
        ok(
            await photos.set_photo(
                session,
                ids["mia"],
                _png(),
                uploaded_by=None,
                now=datetime.now(UTC),
            )
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/volunteers/{ids['mia']}")
        user.find(marker="photo-avatar").click()
        await user.should_see("Remove photo", retries=SLOW)
        user.find("Remove photo", kind=ui.button).click()
        await user.should_see("Remove the photo of Mia Member?", retries=SLOW)
        assert only(user.find(marker=YES)).text == "Remove the photo"
        user.find(marker=YES).click()
        await user.should_see("Photo removed", retries=SLOW)

    async with db_session() as session:
        assert (await photos.versions(session, [ids["mia"]])).get(ids["mia"]) is None


async def test_removing_a_candidate_is_asked(database):
    async with db_session() as session:
        ids = await _parish(session)
        proposal = ok(
            await elections_service.create_proposal(
                session,
                SYSTEM,
                team_id=ids["music"],
                role=TeamRole.second,
                nomination_deadline=mint.today() + timedelta(days=14),
                voting_deadline=mint.today() + timedelta(days=28),
                created_by=ids["admin_u"],
                candidates=[elections_service.CandidateInput(ids["mia"], "steady")],
                today=mint.today(),
            )
        )
        view = ok(
            await elections_service.detail(
                session, SYSTEM, proposal.id, today=mint.today()
            )
        )
        candidate_id = view.candidates[0].candidate.id
        pid = proposal.id

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/elections/{pid}")
        user.find(marker=f"remove-candidate-{candidate_id}").click()
        await user.should_see("Remove Mia Member from the candidates?", retries=SLOW)
        assert only(user.find(marker=YES)).text == "Remove Mia Member"
        user.find(marker=YES).click()
        await user.should_not_see(
            marker=f"remove-candidate-{candidate_id}", retries=SLOW
        )
