"""Bulk where the leader works in bulk (uiux-improvement.md, step 22).

Attendance gets Save all over the changed rows, Schedule someone takes
several names in one transaction, and Take this slot asks first. Each is
a NiceGUI User test over the real page; the mail and the rest of the flow
are proven in tests/test_ui_events.py.
"""

from datetime import datetime, time, timedelta

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import events as event_service

from tests import mint
from tests.actors import as_volunteer
from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.fp_helpers import ok
from tests.test_ui_events import TZ, _parish, _seed_event


async def _past_event_with(ids, *volunteer_keys: str) -> tuple[int, list[int]]:
    """An event two days ago with the given people on its slot; their
    assignment ids in the same order."""
    event_id = await _seed_event(
        ids["liturgy"], slots=[event_service.SlotInput("Lector", 5)]
    )
    async with db_session() as session:
        view = ok(await event_service.detail(session, SYSTEM, event_id))
        assignments = [
            ok(
                await event_service.sign_up(
                    session,
                    as_volunteer(ids[key]),
                    slot_id=view.slots[0].slot.id,
                    volunteer_id=ids[key],
                    now=mint.now(),
                )
            ).id
            for key in volunteer_keys
        ]
        past = datetime.combine(mint.today() - timedelta(days=2), time(9), TZ)
        ok(
            await event_service.update_event(
                session,
                SYSTEM,
                event_id,
                starts_at=past,
                ends_at=past + timedelta(hours=2),
            )
        )
    return event_id, assignments


async def test_save_all_saves_the_changed_rows_together(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id, (mia_a, noor_a, lena_a) = await _past_event_with(
        ids, "mia", "noor", "lena"
    )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        await user.should_see("Attendance")
        # the sheet's order: Lena, Mia, Noor (by last name, then first)
        by_id = lambda e: e.id  # noqa: E731
        boxes = sorted(
            user.find(kind=ui.checkbox, content="attended").elements, key=by_id
        )
        hours = sorted(user.find(kind=ui.number, content="hours").elements, key=by_id)
        assert len(boxes) == 3 and len(hours) == 3
        # nothing changed yet: Save all says so and saves nothing
        user.find(marker="attendance-save-all").click()
        await user.should_see("Nothing changed", retries=SLOW)
        await user.should_not_see("adjusted")

        # two rows change, the third stays as drawn
        boxes[0].value = False  # Lena did not come
        hours[1].value = 3.0  # Mia stayed for the third hour
        user.find(marker="attendance-save-all").click()
        await user.should_see("Attendance saved for 2 people", retries=SLOW)
        badges = [b for b in user.find(kind=ui.badge).elements if b.text == "adjusted"]
        assert len(badges) == 2

    async with db_session() as session:
        rows = {
            a.id: (a.attended_override, a.hours_override)
            for a in [
                await event_service.get_assignment(session, aid)
                for aid in (mia_a, noor_a, lena_a)
            ]
            if a is not None
        }
    assert rows[lena_a][0] is False  # the hours box rides along, as with Save
    assert rows[mia_a][1] == 3
    assert rows[noor_a] == (None, None), "the unchanged row was not written"


async def test_schedule_someone_takes_several_names_or_none(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(
        ids["liturgy"], slots=[event_service.SlotInput("Lector", 2)]
    )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        pick = only(user.find(kind=ui.select, content="Schedule someone"))
        assert pick.props.get("multiple"), "several names at once"
        # three for a slot of two: one transaction, so nobody is added
        pick.value = [ids["mia"], ids["noor"], ids["lena"]]
        user.find("Assign", kind=ui.button).click()
        await user.should_see("full", retries=SLOW)
        await user.should_see("0/2", retries=SLOW)

        pick = only(user.find(kind=ui.select, content="Schedule someone"))
        pick.value = [ids["mia"], ids["noor"]]
        user.find("Assign", kind=ui.button).click()
        await user.should_see("Scheduled 2 people", retries=SLOW)
        await user.should_see("2/2", retries=SLOW)
        await user.should_see("Mia Member")
        await user.should_see("Noor Member")


async def test_take_this_slot_asks_first(database, sim_sent):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])
    async with db_session() as session:
        view = ok(await event_service.detail(session, SYSTEM, event_id))
        a = ok(
            await event_service.sign_up(
                session,
                as_volunteer(ids["mia"]),
                slot_id=view.slots[0].slot.id,
                volunteer_id=ids["mia"],
                now=mint.now(),
            )
        )
        ok(
            await event_service.request_sub(
                session,
                as_volunteer(ids["mia"]),
                assignment_id=a.id,
                requested_by=ids["mia_u"],
                note="out of town",
                now=mint.now(),
            )
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['noor_u']}")
        await user.open(f"/events/{event_id}")
        await user.should_see("Substitutes wanted")
        user.find("Take this slot", kind=ui.button).click()
        await user.should_see("Take the Volunteers slot for Mia Member?", retries=SLOW)
        await user.should_see("they are emailed that you have it")
        user.find(marker="confirm-no").click()
        await user.should_not_see("substitute", retries=SLOW)

        user.find("Take this slot", kind=ui.button).click()
        await user.should_see("Take the Volunteers slot for Mia Member?", retries=SLOW)
        user.find(marker="confirm-yes").click()
        await user.should_see("The slot is yours", retries=SLOW)
        await user.should_see("Noor Member", retries=SLOW)
    assert [m[0] for m in sim_sent] == ["mia@example.org"]
