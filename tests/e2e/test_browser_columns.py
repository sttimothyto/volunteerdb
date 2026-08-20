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


async def open_volunteers(page) -> None:
    await page.goto("/volunteers")
    await ready(page)
    await expect(page.locator("th[data-vdb-col]").first).to_be_visible()


async def test_dragging_a_header_moves_the_column_and_the_order_sticks(seeded, page):
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await open_volunteers(page)
    assert await header_order(page) == DEFAULT_ORDER

    # Phone dropped on Name takes Name's slot, and everything in between
    # shuffles along — an insert, not a swap (ui/column_order.py: reorder)
    await page.drag_and_drop("th[data-vdb-col='phone']", "th[data-vdb-col='name']")
    await expect(page.locator("th[data-vdb-col]").first).to_have_attribute(
        "data-vdb-col", "phone"
    )
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

    await page.drag_and_drop("th[data-vdb-col='email']", "th[data-vdb-col='email']")
    assert await header_order(page) == DEFAULT_ORDER
