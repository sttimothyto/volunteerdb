"""The suggestion dropdown, whose whole behaviour is focus and keys.

ui/search_box.py carries a docstring about a case it could only describe:
"dismissing with Escape leaves the caret where it was, so clicking the box
again fires no focus event" — which is why the box listens for both. A headless
harness has neither a caret nor a click, and NiceGUI's element filters "ignore
a QMenu's open/closed state" (same module), so open-versus-closed is exactly
what it cannot see. A browser can see nothing else.
"""

import re

from playwright.async_api import expect

from .conftest import ready, sign_in

SEARCH_LABEL = "Find volunteers or teams…"  # the dashboard's box
SUGGESTIONS = ".vdb-suggest"  # the QMenu under it (search_box.py)


async def test_suggestions_open_and_close_and_open_again(seeded, page):
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    search = page.get_by_label(SEARCH_LABEL)
    suggestions = page.locator(SUGGESTIONS)

    # under SUGGEST_MIN_CHARS nothing is offered at all
    await search.fill("M")
    await expect(suggestions).to_be_hidden()

    await search.fill("Alv")
    await expect(suggestions).to_be_visible()
    await expect(suggestions.get_by_text("Maria Alvarez")).to_be_visible()

    # Escape dismisses it without clearing the box
    await search.press("Escape")
    await expect(suggestions).to_be_hidden()
    await expect(search).to_have_value("Alv")

    # ... and coming back to a box that still holds a query brings the last
    # suggestions back, though the caret never left and no focus event fires
    await search.click()
    await expect(suggestions).to_be_visible()

    # picking a volunteer opens the side panel rather than navigating
    await suggestions.get_by_text("Maria Alvarez").click()
    drawer = page.locator(".q-drawer")
    await expect(drawer).to_be_visible()
    await expect(drawer.get_by_text("maria@example.org")).to_be_visible()
    await expect(page).to_have_url(re.compile(r":\d+/$"))  # still the dashboard
