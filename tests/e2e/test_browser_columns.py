"""The one link tests/test_ui_column_order.py cannot reach.

That module proves both ends of the gesture — the permutation logic, and the
`vdbColMove` event triggered directly on the table — and says so plainly:
"That leaves exactly one untested link (mousedown-move-drop -> $emit), which is
the part to check by hand in a browser." This is that check, no longer by hand:
a real HTML5 drag across two <th>s, through static/column_drag.js, over the
socket, and back as a re-rendered header row.
"""

import re

from playwright.async_api import expect

from .conftest import icon_button, ready, sign_in

# /volunteers as an admin: the three plain columns, the workload column an
# admin can see, and Status last (ui/volunteers_page.py). None is pinned, so
# every one of them is both a drag source and a drop target.
DEFAULT_ORDER = ["name", "email", "phone", "workload", "status"]


async def header_order(page) -> list[str]:
    """The draggable header cells, left to right, by column name."""
    return await page.locator("th[data-vdb-col]").evaluate_all(
        "cells => cells.map(cell => cell.dataset.vdbCol)"
    )


EVENT_LOG = """
() => {
    window.__vdbEvents = [];
    for (const type of ["dragstart", "dragenter", "dragover", "drop", "dragend"]) {
        document.addEventListener(type, (e) => {
            const cell = e.target instanceof Element ? e.target.closest("th") : null;
            window.__vdbEvents.push(type + ":" + (cell ? cell.dataset.vdbCol : "-"));
        }, true);
    }
}
"""


async def drag_header(page, moved: str, target: str) -> None:
    """Drag one header cell onto another, the way Playwright's own guide says
    a page that relies on dragover must be driven: press on the source, move
    onto the target twice, release. A single move (what the one-call drag
    does) reaches a drag session that started late only some of the time;
    when it does not, nothing permitted the drop and the drop is lost."""
    await page.evaluate(EVENT_LOG)
    await page.locator(f"th[data-vdb-col='{moved}']").hover()
    await page.mouse.down()
    dst = page.locator(f"th[data-vdb-col='{target}']")
    await dst.hover()
    await dst.hover()
    await page.mouse.up()


async def drag_events(page) -> list[str]:
    return await page.evaluate("() => window.__vdbEvents || []")


async def open_volunteers(page) -> None:
    await page.goto("/volunteers")
    await ready(page)
    await expect(page.locator("th[data-vdb-col]").first).to_be_visible()
    # And the drag script itself. It is a separate file the page loads after
    # the handshake NiceGUI reports, and a drag that starts before it has run
    # has no listeners to reach: the drop lands nowhere, and nothing on the
    # page says so. Seen in about a quarter of runs, clustered at a cold
    # start. The flag is the one the script sets to make itself idempotent.
    await page.wait_for_function("window.__vdbColumnDrag === true")
    await expect(page.locator("td").first).to_be_visible()


async def test_dragging_a_header_moves_the_column_and_the_order_sticks(seeded, page):
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await open_volunteers(page)
    assert await header_order(page) == DEFAULT_ORDER

    # Phone dropped on Name takes Name's slot, and everything in between
    # shuffles along — an insert, not a swap (ui/column_order.py: reorder)
    await drag_header(page, "phone", "name")
    try:
        await expect(page.locator("th[data-vdb-col]").first).to_have_attribute(
            "data-vdb-col", "phone"
        )
    except AssertionError as exc:
        raise AssertionError(f"drag events: {await drag_events(page)}") from exc
    assert await header_order(page) == ["phone", "name", "email", "workload", "status"]

    # the drag markers the stylesheet hangs off are cleaned up after the drop,
    # or the moved header would stay highlighted for the rest of the session
    assert await page.locator("[data-vdb-drag], [data-vdb-drop]").count() == 0

    # held in app.storage.user, so it outlives the page it was arranged on
    await open_volunteers(page)
    assert await header_order(page) == ["phone", "name", "email", "workload", "status"]

    # ... but not the sitting: clear_session() drops the column order while
    # keeping the preferences that belong to the browser rather than the login
    await icon_button(page, "logout").click()
    await expect(page).to_have_url(re.compile(r"/login$"))
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await open_volunteers(page)
    assert await header_order(page) == DEFAULT_ORDER


async def test_a_column_dropped_on_itself_changes_nothing(seeded, page):
    """The no-op the JS layer is supposed to swallow before the socket ever
    hears about it (column_drag.js: "drop on self: not a target")."""
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await open_volunteers(page)

    await drag_header(page, "email", "email")
    assert await header_order(page) == DEFAULT_ORDER
