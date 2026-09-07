"""A button that is working says so.

widgets.busy wraps a click handler: while it runs, the button that was
clicked carries Quasar's `loading` prop (the spinner) and is disabled (no
second click), and both come off when it returns -- or stay, harmlessly, on
a button the reload already took away. The wrapper is what forms.actions
puts on every dialog's primary, and what the sync, fetch, import and bulk-
invite buttons carry.

The simulation cannot see a spinner, and a real handler finishes before
anything could look; so this drives the wrapper by hand with a handler
that waits to be released."""

import asyncio

from nicegui import ui
from nicegui.events import ClickEventArguments
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.permissions import SYSTEM
from volunteerdb.services import users
from volunteerdb.ui.widgets import busy

from tests import mint
from tests.conftest import SIM_MAIN, db_session
from tests.fp_helpers import ok


async def test_the_button_spins_and_takes_no_second_click_while_the_handler_runs(
    database,
):
    async with db_session() as session:
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        admin_id = admin.id

    release = asyncio.Event()
    calls: list[str] = []

    async def slow() -> None:
        calls.append("started")
        await release.wait()
        calls.append("done")

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        with user:
            button = ui.button("Sync now", on_click=busy(slow))
            click = ClickEventArguments(sender=button, client=user.client)
            task = asyncio.create_task(busy(slow)(click))
            await asyncio.sleep(0.05)
            assert calls == ["started"]
            assert "loading" in button.props, "the spinner is on while it works"
            assert not button.enabled, "and a second click is refused"

            release.set()
            await task
            assert calls == ["started", "done"]
            assert "loading" not in button.props, "the spinner comes off"
            assert button.enabled, "and the button takes a click again"


async def test_a_handler_that_takes_the_event_gets_it(database):
    """NiceGUI calls a handler with the event when it asks for one; the
    wrapper does the same, so `lambda _, aid=...` handlers keep working."""
    async with db_session() as session:
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        admin_id = admin.id

    seen: list[object] = []
    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{admin_id}")
        with user:
            button = ui.button("Go")
            click = ClickEventArguments(sender=button, client=user.client)
            await busy(lambda e, tag="bound": seen.append((tag, e.sender)))(click)
            await busy(lambda: seen.append("bare"))(click)
    assert seen == [("bound", button), "bare"]
