"""Dialog field rows stack below 40 rem (uiux-improvement.md, step 29).

The cascade is the browser's to prove (tests/e2e/test_browser_layout.py);
this holds the rows that must carry the class: the new and edit event
dialogs' date row, the slot rows, and the availability card's row.
"""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from tests.conftest import SIM_MAIN, SLOW, db_session, only
from tests.test_ui_events import _parish, _seed_event


def _row_of(element: ui.element) -> ui.element:
    parent = element.parent_slot.parent if element.parent_slot else None
    assert parent is not None
    return parent


async def test_the_rows_that_stack_carry_the_class(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/events")
        user.find("New event", kind=ui.button).click()
        await user.should_see("Starts (HH:MM)", retries=SLOW)
        start = only(user.find(kind=ui.input, content="Starts (HH:MM)"))
        assert "vdb-fields" in _row_of(start).classes
        slot = only(user.find(kind=ui.input, content="Slot"))
        assert "vdb-fields" in _row_of(slot).classes

        await user.open(f"/events/{event_id}")
        user.find("Edit", kind=ui.button).click()
        await user.should_see("Starts (HH:MM)", retries=SLOW)
        start = only(user.find(kind=ui.input, content="Starts (HH:MM)"))
        assert "vdb-fields" in _row_of(start).classes

        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{event_id}")
        note = only(user.find(kind=ui.input, content="Note (optional)"))
        assert "vdb-fields" in _row_of(note).classes
